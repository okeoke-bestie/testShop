"""Сервис оформления заказов — вся бизнес-логика проекта.

Три вещи, которые здесь решаются и о которых спрашивают на собеседовании:

  1. Резервирование товара без оверселлинга при параллельных заказах.
  2. Идемпотентность: повтор запроса не создаёт второй заказ.
  3. Компенсация: если что-то упало посреди оформления, остатки
     возвращаются на склад, а не «зависают».
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from ..errors import (
    Conflict,
    IdempotencyConflict,
    NotFound,
    OutOfStock,
    ValidationError,
)
from ..models import Order, OrderLine, OrderStatus

logger = logging.getLogger(__name__)

MAX_LINES = 50
MAX_QUANTITY_PER_LINE = 100


@dataclass(frozen=True)
class OrderRequest:
    """Запрос на создание заказа.

    `items` — список (product_id, quantity). Отдельный тип, а не словарь:
    ошибка в имени ключа находится при разборе запроса, а не через час
    в логах.
    """

    customer_id: int
    items: list[tuple[int, int]]
    idempotency_key: str | None = None

    def fingerprint(self) -> str:
        """Отпечаток запроса для проверки идемпотентности.

        Нужен, чтобы отличить честный повтор (сеть моргнула, клиент
        отправил тот же заказ ещё раз) от переиспользования ключа для
        ДРУГОГО заказа. В первом случае возвращаем прежний результат,
        во втором — ошибку: иначе клиент получит не тот заказ, который
        просил, и не узнает об этом.

        Товары сортируются: [(1,2),(3,4)] и [(3,4),(1,2)] — один и тот же
        заказ, и отпечаток у них обязан совпасть.
        """
        payload = {
            "customer_id": self.customer_id,
            "items": sorted(self.items),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class OrderService:
    products: object
    orders: object
    db: object

    # ---------- проверки ----------

    def _validate(self, request: OrderRequest) -> None:
        """Проверки входа — до любых обращений к базе.

        Смысл порядка: отбросить заведомо неверный запрос дёшево,
        не занимая соединение с базой и не открывая транзакцию.
        Ограничения сверху (не больше 50 позиций) — защита от запроса,
        который положит сервис: без них можно прислать миллион позиций.
        """
        if not request.items:
            raise ValidationError("заказ не может быть пустым")
        if len(request.items) > MAX_LINES:
            raise ValidationError(
                f"слишком много позиций, максимум {MAX_LINES}", count=len(request.items)
            )

        seen = set()
        for product_id, quantity in request.items:
            if quantity <= 0:
                raise ValidationError("количество должно быть положительным")
            if quantity > MAX_QUANTITY_PER_LINE:
                raise ValidationError(
                    f"не больше {MAX_QUANTITY_PER_LINE} штук одной позиции"
                )
            if product_id in seen:
                # Дубли пришлось бы складывать, и тогда проверка
                # количества выше обходится: две строки по 100 штук.
                raise ValidationError("повторяющийся товар в заказе", product_id=product_id)
            seen.add(product_id)

    # ---------- идемпотентность ----------

    def _check_idempotency(self, request: OrderRequest) -> Order | None:
        if not request.idempotency_key:
            return None
        existing = self.orders.find_by_idempotency_key(request.idempotency_key)
        if existing is None:
            return None
        order, stored_hash = existing
        if stored_hash and stored_hash != request.fingerprint():
            raise IdempotencyConflict(
                "ключ идемпотентности уже использован для другого заказа",
                idempotency_key=request.idempotency_key,
            )
        logger.info("Повтор по ключу идемпотентности, возвращаю заказ %s", order.id)
        return order

    # ---------- основной сценарий ----------

    def create_order(self, request: OrderRequest) -> Order:
        """Создаёт заказ, резервируя товар.

        Порядок шагов неслучаен:

        1. валидация — дёшево, до базы;
        2. проверка идемпотентности — чтобы не делать работу дважды;
        3. транзакция: читаем товары, резервируем остатки, пишем заказ.

        Резерв идёт ВНУТРИ транзакции и атомарным UPDATE. Если хоть одна
        позиция не зарезервировалась, вся транзакция откатывается —
        и уже списанные остатки возвращаются автоматически, без ручной
        компенсации. Это и есть главный аргумент в пользу того, чтобы
        держать связанные изменения в одной транзакции.
        """
        self._validate(request)

        existing = self._check_idempotency(request)
        if existing is not None:
            return existing

        product_ids = [pid for pid, _ in request.items]

        with self.db.transaction():
            # Один запрос на все товары, а не по одному в цикле.
            found = self.products.get_many(product_ids)
            missing = [pid for pid in product_ids if pid not in found]
            if missing:
                raise NotFound("товар не найден", product_ids=missing)

            lines: list[OrderLine] = []
            for product_id, quantity in request.items:
                product = found[product_id]
                if not self.products.reserve_stock(product_id, quantity):
                    # Откат транзакции вернёт остатки, зарезервированные
                    # на предыдущих итерациях. Вручную ничего
                    # компенсировать не нужно.
                    raise OutOfStock(
                        f"недостаточно товара «{product.title}»",
                        product_id=product_id,
                        requested=quantity,
                        available=product.stock,
                    )
                lines.append(
                    OrderLine(
                        product_id=product_id,
                        sku=product.sku,
                        quantity=quantity,
                        price_kopecks=product.price_kopecks,
                    )
                )

            order = Order(
                id=None,
                customer_id=request.customer_id,
                lines=lines,
                idempotency_key=request.idempotency_key,
            )
            try:
                order_id = self.orders.create(order, request_hash=request.fingerprint())
            except Exception as exc:
                # Уникальный индекс по ключу идемпотентности сработал:
                # параллельный запрос с тем же ключом успел раньше.
                # Это не ошибка, а именно то, ради чего индекс и стоит.
                if "idempotency" in str(exc).lower() or "unique" in str(exc).lower():
                    raise Conflict(
                        "заказ с этим ключом уже создаётся",
                        idempotency_key=request.idempotency_key,
                    ) from exc
                raise

        order.id = order_id
        logger.info(
            "Создан заказ %s для клиента %s на сумму %s коп.",
            order_id, request.customer_id, order.total_kopecks,
        )
        return order

    def cancel_order(self, order_id: int) -> Order:
        """Отмена заказа с возвратом остатков.

        Возврат и смена статуса — в одной транзакции: иначе возможно
        состояние «остатки вернули, статус не поменяли», и следующая
        отмена вернёт их второй раз.
        """
        with self.db.transaction():
            order = self.orders.get(order_id)
            if not order.can_cancel():
                raise ValidationError(
                    f"заказ в статусе {order.status.value} отменить нельзя",
                    order_id=order_id,
                )
            for line in order.lines:
                self.products.release_stock(line.product_id, line.quantity)
            self.orders.set_status(order_id, OrderStatus.CANCELLED)

        order.status = OrderStatus.CANCELLED
        logger.info("Заказ %s отменён, остатки возвращены", order_id)
        return order
