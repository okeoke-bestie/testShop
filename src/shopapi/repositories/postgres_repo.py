"""Репозитории для PostgreSQL.

Второй репозиторий рядом с sqlite-версией — ровно то, ради чего
слои разделены: сервисы и доменные модели не изменились ни на строку.

Что отличается от sqlite, кроме плейсхолдеров:

* `RETURNING` работает так же, но здесь он ещё и возвращает новый
  остаток — сервис получает актуальное значение без повторного SELECT;
* `ON CONFLICT DO NOTHING` даёт атомарную вставку по ключу
  идемпотентности без гонки и без исключения;
* `SELECT ... FOR UPDATE` доступен для случаев, где нужна блокировка
  строки на время транзакции;
* `unnest` позволяет вставить все строки заказа одним запросом.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime

from ..errors import Conflict, NotFound, ValidationError
from ..models import Order, OrderLine, OrderStatus, Page, Product


def encode_cursor(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_cursor(cursor: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("некорректный курсор", cursor=cursor) from exc


def row_to_product(row: dict) -> Product:
    return Product(
        id=row["id"],
        sku=row["sku"],
        title=row["title"],
        price_kopecks=row["price_kopecks"],
        stock=row["stock"],
        version=row["version"],
    )


@dataclass
class PgProductRepository:
    db: object

    # ---------- чтение ----------

    def add(
        self,
        sku: str,
        title: str,
        price_kopecks: int,
        stock: int,
        description: str = "",
        category: str = "Прочее",
        emoji: str = "📦",
        platform: str = "",
        genre: str = "",
        developer: str = "",
        year: int = 0,
        specs: str = "{}",
    ) -> Product:
        row = self.db.query_one(
            """
            INSERT INTO products (sku, title, description, category, emoji,
                                  platform, genre, developer, year, specs,
                                  price_kopecks, stock)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (sku, title, description, category, emoji,
             platform, genre, developer, year, specs, price_kopecks, stock),
        )
        return row_to_product(row)

    def get(self, product_id: int) -> Product:
        row = self.db.query_one("SELECT * FROM products WHERE id = %s", (product_id,))
        if row is None:
            raise NotFound("товар не найден", product_id=product_id)
        return row_to_product(row)

    def get_full(self, product_id: int) -> dict:
        """Полная карточка для витрины: с описанием и категорией."""
        row = self.db.query_one("SELECT * FROM products WHERE id = %s", (product_id,))
        if row is None:
            raise NotFound("товар не найден", product_id=product_id)
        return dict(row)

    def get_many(self, ids: list[int]) -> dict[int, Product]:
        """Пачкой одним запросом — лечение N+1.

        `= ANY(%s)` вместо `IN (...)`: список передаётся ОДНИМ параметром,
        не нужно строить плейсхолдеры по числу элементов. Заодно план
        запроса не меняется от длины списка, и Postgres может его
        переиспользовать.
        """
        if not ids:
            return {}
        rows = self.db.query_all("SELECT * FROM products WHERE id = ANY(%s)", (ids,))
        return {row["id"]: row_to_product(row) for row in rows}

    def get_by_sku(self, sku: str) -> dict:
        """Товар по артикулу — для адреса /product/SKU."""
        row = self.db.query_one("SELECT * FROM products WHERE sku = %s", (sku,))
        if row is None:
            raise NotFound("товар не найден", sku=sku)
        return dict(row)

    def recommendations(self, product_id: int, limit: int = 4) -> list[dict]:
        """Похожие товары. Логика та же, что у sqlite-версии.

        Разница только в диалекте: в PostgreSQL булево выражение
        сортируется само, и `DESC` ставит true первым.
        """
        row = self.db.query_one(
            "SELECT category, platform FROM products WHERE id = %s", (product_id,)
        )
        if row is None:
            raise NotFound("товар не найден", product_id=product_id)

        rows = self.db.query_all(
            """
            SELECT * FROM products
             WHERE id <> %s
             ORDER BY (category = %s AND platform = %s) DESC,
                      (platform = %s) DESC,
                      (stock > 0) DESC,
                      title
             LIMIT %s
            """,
            (product_id, row["category"], row["platform"], row["platform"], limit),
        )
        return [dict(r) for r in rows]

    def list_all(self, category: str | None = None, search: str | None = None,
                 platform: str | None = None) -> list[dict]:
        """Витринный список: всё, что нужно карточке товара.

        Фильтры складываются через AND, значения уходят параметрами:
        кавычка в поисковой строке остаётся кавычкой, а не становится SQL.
        """
        clauses = []
        params: list = []
        if category and category != "all":
            clauses.append("category = %s")
            params.append(category)
        if platform and platform != "all":
            clauses.append("platform = %s")
            params.append(platform)
        if search and search.strip():
            # ILIKE — регистронезависимый поиск. На большом каталоге
            # нужен полнотекстовый индекс (GIN + tsvector), потому что
            # ведущий процент не даёт использовать обычный B-tree.
            clauses.append("(title ILIKE %s OR description ILIKE %s "
                           "OR developer ILIKE %s)")
            pattern = f"%{search.strip()}%"
            params += [pattern, pattern, pattern]

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query_all(
            f"SELECT * FROM products {where} ORDER BY title, id", tuple(params)
        )
        return [dict(r) for r in rows]

    def categories(self) -> list[dict]:
        rows = self.db.query_all(
            """
            SELECT category, COUNT(*) AS count, SUM(stock) AS total_stock
              FROM products
             GROUP BY category
             ORDER BY category
            """
        )
        return [dict(r) for r in rows]

    def platforms(self) -> list[dict]:
        """Платформы со счётчиками — вкладки каталога.

        Пустая платформа отсеивается в базе, а не в вёрстке: иначе
        вкладка без названия доезжает до экрана.
        """
        rows = self.db.query_all(
            """
            SELECT platform, COUNT(*) AS count, SUM(stock) AS total_stock
              FROM products
             WHERE platform <> ''
             GROUP BY platform
             ORDER BY COUNT(*) DESC, platform
            """
        )
        return [dict(r) for r in rows]

    def list_page(self, limit: int = 20, cursor: str | None = None) -> Page:
        """Keyset-пагинация. Сравнение кортежей `(title, id) > (%s, %s)`
        Postgres поддерживает напрямую и умеет использовать составной
        индекс по (title, id)."""
        if limit <= 0 or limit > 100:
            raise ValidationError("limit должен быть от 1 до 100", limit=limit)

        if cursor:
            position = decode_cursor(cursor)
            rows = self.db.query_all(
                """
                SELECT * FROM products
                 WHERE (title, id) > (%s, %s)
                 ORDER BY title, id
                 LIMIT %s
                """,
                (position["title"], position["id"], limit + 1),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM products ORDER BY title, id LIMIT %s", (limit + 1,)
            )

        has_more = len(rows) > limit
        products = [row_to_product(r) for r in rows[:limit]]
        next_cursor = None
        if has_more and products:
            last = products[-1]
            next_cursor = encode_cursor({"title": last.title, "id": last.id})
        return Page(items=products, next_cursor=next_cursor)

    # ---------- остатки ----------

    def reserve_stock(self, product_id: int, quantity: int) -> bool:
        """Атомарное списание. Главный запрос всего проекта.

        Проверка и изменение — одной командой, условие проверяет сама
        база под блокировкой строки. Два параллельных заказа на последний
        товар: один получит строку, второй — пустой результат.

        `RETURNING stock` возвращает НОВЫЙ остаток, поэтому вызывающему
        коду не нужен повторный SELECT, чтобы узнать, сколько осталось.
        """
        if quantity <= 0:
            raise ValidationError("количество должно быть положительным")
        row = self.db.query_one(
            """
            UPDATE products
               SET stock = stock - %s, version = version + 1
             WHERE id = %s AND stock >= %s
            RETURNING stock
            """,
            (quantity, product_id, quantity),
        )
        return row is not None

    def reserve_stock_unsafe(self, product_id: int, quantity: int) -> bool:
        """Наивная версия с гонкой — только для демонстрации.

        Читает, проверяет и пишет тремя отдельными шагами. Между ними
        вклиниваются конкуренты, и товар продаётся несколько раз.
        """
        row = self.db.query_one("SELECT stock FROM products WHERE id = %s", (product_id,))
        if row is None or row["stock"] < quantity:
            return False
        self.db.execute(
            "UPDATE products SET stock = %s WHERE id = %s",
            (row["stock"] - quantity, product_id),
        )
        return True

    def reserve_stock_for_update(self, product_id: int, quantity: int) -> bool:
        """Альтернатива через явную блокировку строки.

        `FOR UPDATE` блокирует строку до конца транзакции: конкурент
        ждёт на SELECT, а не проигрывает на UPDATE. Работает корректно,
        но держит блокировку дольше — всё время между чтением и записью.

        Уместно, когда между проверкой и изменением нужна сложная логика,
        которую не выразить одним UPDATE.
        """
        row = self.db.query_one(
            "SELECT stock FROM products WHERE id = %s FOR UPDATE", (product_id,)
        )
        if row is None or row["stock"] < quantity:
            return False
        self.db.execute(
            "UPDATE products SET stock = stock - %s, version = version + 1 WHERE id = %s",
            (quantity, product_id),
        )
        return True

    def release_stock(self, product_id: int, quantity: int) -> None:
        self.db.execute(
            "UPDATE products SET stock = stock + %s, version = version + 1 WHERE id = %s",
            (quantity, product_id),
        )

    def set_stock(self, sku: str, quantity: int) -> dict | None:
        row = self.db.query_one(
            """
            UPDATE products SET stock = %s, version = version + 1
             WHERE sku = %s
            RETURNING *
            """,
            (quantity, sku),
        )
        return dict(row) if row else None

    def delete_all(self) -> None:
        self.db.execute("DELETE FROM products")


@dataclass
class PgOrderRepository:
    db: object

    def create(self, order: Order, request_hash: str | None = None) -> int:
        """Создаёт заказ вместе со строками.

        `ON CONFLICT ... DO NOTHING` — атомарная проверка уникальности
        прямо во вставке: без гонки, без исключения и без предварительного
        SELECT. Пустой результат означает, что ключ уже занят — кто-то
        успел раньше.

        Тонкость, на которой это сразу упало при первом запуске: индекс
        по ключу ЧАСТИЧНЫЙ (`WHERE idempotency_key IS NOT NULL`), и для
        вывода конфликта Postgres требует повторить то же условие.
        Без него — ошибка:

            there is no unique or exclusion constraint matching
            the ON CONFLICT specification

        Логика такая: частичных индексов по одной колонке может быть
        несколько, с разными условиями, и база не угадывает, какой
        из них имелся в виду.
        """
        row = self.db.query_one(
            """
            INSERT INTO orders (customer_id, status, created_at, idempotency_key, request_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
            DO NOTHING
            RETURNING id
            """,
            (
                order.customer_id,
                order.status.value,
                order.created_at,
                order.idempotency_key,
                request_hash,
            ),
        )
        if row is None:
            raise Conflict(
                "заказ с этим ключом уже создаётся",
                idempotency_key=order.idempotency_key,
            )

        order_id = row["id"]
        # Все строки заказа одной вставкой: `unnest` разворачивает
        # массивы в строки. Цикл с INSERT дал бы N запросов.
        if order.lines:
            self.db.execute(
                """
                INSERT INTO order_lines (order_id, product_id, sku, title, quantity, price_kopecks)
                SELECT %s, * FROM unnest(
                    %s::bigint[], %s::text[], %s::text[], %s::int[], %s::bigint[]
                )
                """,
                (
                    order_id,
                    [line.product_id for line in order.lines],
                    [line.sku for line in order.lines],
                    [getattr(line, "title", line.sku) for line in order.lines],
                    [line.quantity for line in order.lines],
                    [line.price_kopecks for line in order.lines],
                ),
            )
        return order_id

    def get(self, order_id: int) -> Order:
        row = self.db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
        if row is None:
            raise NotFound("заказ не найден", order_id=order_id)
        lines = self._lines_for([order_id]).get(order_id, [])
        return self._row_to_order(row, lines)

    def get_full(self, order_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
        if row is None:
            raise NotFound("заказ не найден", order_id=order_id)
        lines = self.db.query_all(
            "SELECT * FROM order_lines WHERE order_id = %s ORDER BY id", (order_id,)
        )
        payload = dict(row)
        payload["lines"] = [dict(line) for line in lines]
        payload["total_kopecks"] = sum(
            line["quantity"] * line["price_kopecks"] for line in lines
        )
        return payload

    def find_by_idempotency_key(self, key: str) -> tuple[Order, str | None] | None:
        row = self.db.query_one(
            "SELECT * FROM orders WHERE idempotency_key = %s", (key,)
        )
        if row is None:
            return None
        lines = self._lines_for([row["id"]]).get(row["id"], [])
        return self._row_to_order(row, lines), row["request_hash"]

    def list_for_customer(self, customer_id: int, limit: int = 50) -> list[Order]:
        """Два запроса независимо от числа заказов."""
        rows = self.db.query_all(
            "SELECT * FROM orders WHERE customer_id = %s ORDER BY id DESC LIMIT %s",
            (customer_id, limit),
        )
        if not rows:
            return []
        lines_by_order = self._lines_for([r["id"] for r in rows])
        return [self._row_to_order(r, lines_by_order.get(r["id"], [])) for r in rows]

    def list_for_customer_nplus1(self, customer_id: int, limit: int = 50) -> list[Order]:
        """Наивная версия — для демонстрации N+1."""
        rows = self.db.query_all(
            "SELECT * FROM orders WHERE customer_id = %s ORDER BY id DESC LIMIT %s",
            (customer_id, limit),
        )
        result = []
        for row in rows:
            lines = self._lines_for([row["id"]]).get(row["id"], [])
            result.append(self._row_to_order(row, lines))
        return result

    def recent(self, limit: int = 20) -> list[dict]:
        """Последние заказы для витрины."""
        rows = self.db.query_all(
            """
            SELECT o.id, o.status, o.created_at,
                   COALESCE(SUM(l.quantity * l.price_kopecks), 0) AS total_kopecks,
                   COALESCE(SUM(l.quantity), 0) AS items
              FROM orders o
              LEFT JOIN order_lines l ON l.order_id = o.id
             GROUP BY o.id
             ORDER BY o.id DESC
             LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    def set_status(self, order_id: int, status: OrderStatus) -> None:
        self.db.execute(
            "UPDATE orders SET status = %s WHERE id = %s", (status.value, order_id)
        )

    # ---------- служебное ----------

    def _lines_for(self, order_ids: list[int]) -> dict[int, list[OrderLine]]:
        if not order_ids:
            return {}
        rows = self.db.query_all(
            "SELECT * FROM order_lines WHERE order_id = ANY(%s) ORDER BY id",
            (order_ids,),
        )
        result: dict[int, list[OrderLine]] = {}
        for row in rows:
            result.setdefault(row["order_id"], []).append(
                OrderLine(
                    product_id=row["product_id"],
                    sku=row["sku"],
                    quantity=row["quantity"],
                    price_kopecks=row["price_kopecks"],
                )
            )
        return result

    @staticmethod
    def _row_to_order(row: dict, lines: list[OrderLine]) -> Order:
        created = row["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return Order(
            id=row["id"],
            customer_id=row["customer_id"],
            lines=lines,
            status=OrderStatus(row["status"]),
            created_at=created,
            idempotency_key=row["idempotency_key"],
        )
