"""Репозитории на sqlite3.

Здесь собраны три приёма, которые чаще всего спрашивают про работу
с базой, и каждый оформлен так, чтобы его можно было показать и замерить:

  1. атомарное списание остатка одним UPDATE — защита от оверселлинга;
  2. keyset-пагинация вместо OFFSET;
  3. загрузка связанных строк одним запросом вместо N+1.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from ..db import Database
from ..errors import NotFound, ValidationError
from ..models import Order, OrderLine, OrderStatus, Page, Product


def encode_cursor(payload: dict) -> str:
    """Курсор — это непрозрачная для клиента строка.

    Base64 здесь не про безопасность, а про контракт: клиент не должен
    разбирать курсор и строить свои. Иначе поменять порядок сортировки
    без слома клиентов станет невозможно.
    """
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_cursor(cursor: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("некорректный курсор", cursor=cursor) from exc


def row_to_product(row) -> Product:
    return Product(
        id=row["id"],
        sku=row["sku"],
        title=row["title"],
        price_kopecks=row["price_kopecks"],
        stock=row["stock"],
        version=row["version"],
    )


@dataclass
class SqliteProductRepository:
    db: Database

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
        """Сигнатура повторяет постгресовую дословно.

        Это не формальность: витрина и загрузчик каталога вызывают
        репозиторий, ничего не зная о базе под ним. Разошлись сигнатуры —
        и приложение работает на одной базе и падает на другой.
        Это проверяется тестом `test_repositories_agree_on_signatures`.
        """
        cursor = self.db.execute(
            """
            INSERT INTO products (sku, title, description, category, emoji,
                                  platform, genre, developer, year, specs,
                                  price_kopecks, stock)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sku, title, description, category, emoji,
             platform, genre, developer, year, specs, price_kopecks, stock),
        )
        return self.get(cursor.lastrowid)

    def get(self, product_id: int) -> Product:
        row = self.db.query_one("SELECT * FROM products WHERE id = ?", (product_id,))
        if row is None:
            raise NotFound("товар не найден", product_id=product_id)
        return row_to_product(row)

    def get_full(self, product_id: int) -> dict:
        """Полная карточка для витрины: с описанием и категорией."""
        row = self.db.query_one("SELECT * FROM products WHERE id = ?", (product_id,))
        if row is None:
            raise NotFound("товар не найден", product_id=product_id)
        return dict(row)

    def get_by_sku(self, sku: str) -> dict:
        """Товар по артикулу — для адреса /product/SKU.

        В адресе стоит артикул, а не числовой id: ссылка читается,
        а сам id — деталь хранения, которой незачем торчать наружу.
        """
        row = self.db.query_one("SELECT * FROM products WHERE sku = ?", (sku,))
        if row is None:
            raise NotFound("товар не найден", sku=sku)
        return dict(row)

    def recommendations(self, product_id: int, limit: int = 4) -> list[dict]:
        """Похожие товары: та же платформа и категория, потом просто платформа.

        Сортировка сделана выражением в ORDER BY, а не двумя запросами
        с объединением: база сама расставит приоритет за один проход.
        Сам товар исключён — рекомендовать страницу саму на себя
        выглядит как ошибка, и это она и есть.

        Товары без остатка уходят в конец: рекомендовать то, что нельзя
        купить, — плохой совет.
        """
        row = self.db.query_one(
            "SELECT category, platform FROM products WHERE id = ?", (product_id,)
        )
        if row is None:
            raise NotFound("товар не найден", product_id=product_id)

        rows = self.db.query_all(
            """
            SELECT * FROM products
             WHERE id <> ?
             ORDER BY (category = ? AND platform = ?) DESC,
                      (platform = ?) DESC,
                      (stock > 0) DESC,
                      title
             LIMIT ?
            """,
            (product_id, row["category"], row["platform"], row["platform"], limit),
        )
        return [dict(r) for r in rows]

    def list_all(self, category: str | None = None, search: str | None = None,
                 platform: str | None = None) -> list[dict]:
        """Витринный список: всё, что нужно карточке товара.

        Фильтры складываются через AND и собираются списком, а не
        конкатенацией строк: значения уходят параметрами, поэтому
        кавычка в поисковом запросе — это просто кавычка, а не SQL.
        """
        clauses: list[str] = []
        params: list = []
        if category and category != "all":
            clauses.append("category = ?")
            params.append(category)
        if platform and platform != "all":
            clauses.append("platform = ?")
            params.append(platform)
        if search and search.strip():
            # `pylower` — своя функция, зарегистрированная в соединении
            # (см. db.py). Встроенные LIKE и LOWER в sqlite знают только
            # латиницу, и «Консоль» не находилась бы по «консоль».
            clauses.append("(pylower(title) LIKE ? OR pylower(description) LIKE ? "
                           "OR pylower(developer) LIKE ?)")
            pattern = f"%{search.strip().lower()}%"
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
        вкладка с пустым названием доезжает до экрана, и это заметит
        пользователь, а не тест.
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

    def set_stock(self, sku: str, quantity: int) -> dict | None:
        row = self.db.query_one(
            """
            UPDATE products SET stock = ?, version = version + 1
             WHERE sku = ?
            RETURNING *
            """,
            (quantity, sku),
        )
        return dict(row) if row else None

    def delete_all(self) -> None:
        self.db.execute("DELETE FROM products")

    def get_many(self, ids: list[int]) -> dict[int, Product]:
        """Пачкой, а не по одному.

        Это и есть лечение N+1: вместо `for id in ids: get(id)` — один
        запрос с IN. Плейсхолдеры строятся по числу элементов, значения
        передаются параметрами: подставлять их в строку запроса нельзя,
        это прямая дорога к SQL-инъекции.
        """
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.db.query_all(
            f"SELECT * FROM products WHERE id IN ({placeholders})", tuple(ids)
        )
        return {row["id"]: row_to_product(row) for row in rows}

    def list_page(self, limit: int = 20, cursor: str | None = None) -> Page:
        """Keyset-пагинация: страница берётся «после последней записи».

        Почему не OFFSET: `LIMIT 20 OFFSET 10000` заставляет базу
        прочитать и выбросить 10000 строк — чем дальше страница, тем
        медленнее, и на глубоких страницах это становится невыносимо.
        Вторая беда OFFSET — сдвиг: если между запросами кто-то вставил
        запись, одна строка покажется дважды, а другая пропадёт.

        Keyset берёт строки строго после известной позиции. Сложность
        не зависит от глубины, сдвига не происходит. Цена — нельзя
        прыгнуть на «страницу 50», только листать последовательно.

        Сортировка по (title, id), а не только по title: title
        не уникален, и при равных значениях порядок не определён —
        строки начнут «прыгать» между страницами. Последним ключом
        сортировки всегда должно быть что-то уникальное.
        """
        if limit <= 0 or limit > 100:
            raise ValidationError("limit должен быть от 1 до 100", limit=limit)

        if cursor:
            position = decode_cursor(cursor)
            rows = self.db.query_all(
                """
                SELECT * FROM products
                WHERE (title, id) > (?, ?)
                ORDER BY title, id
                LIMIT ?
                """,
                (position["title"], position["id"], limit + 1),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM products ORDER BY title, id LIMIT ?", (limit + 1,)
            )

        # Берём на одну запись больше, чем нужно: наличие «лишней» строки
        # и есть признак того, что дальше что-то есть. Иначе пришлось бы
        # делать отдельный COUNT(*), а это второй проход по таблице.
        has_more = len(rows) > limit
        rows = rows[:limit]
        products = [row_to_product(r) for r in rows]

        next_cursor = None
        if has_more and products:
            last = products[-1]
            next_cursor = encode_cursor({"title": last.title, "id": last.id})
        return Page(items=products, next_cursor=next_cursor)

    def reserve_stock(self, product_id: int, quantity: int) -> bool:
        """Атомарное списание остатка. Ключевой метод всего проекта.

        Наивная версия выглядит так и СОДЕРЖИТ ГОНКУ:

            product = get(product_id)          # прочитали stock = 1
            if product.stock >= quantity:      # проверили: хватает
                update(stock = product.stock - quantity)

        Между чтением и записью успевает вклиниться второй поток:
        оба читают stock = 1, оба проверяют, оба списывают — и товар
        продан дважды. Ошибка не воспроизводится на одном потоке
        и всплывает в проде под нагрузкой.

        Здесь проверка и изменение выполняются ОДНОЙ командой: условие
        `stock >= ?` проверяет сама база в момент записи, под блокировкой
        строки. Второй поток либо увидит уже уменьшенный остаток
        и обновит ноль строк, либо подождёт. Возврат — сколько строк
        реально изменилось; ноль означает «не хватило».

        Тот же приём работает в Postgres и MySQL слово в слово.
        Альтернатива — SELECT ... FOR UPDATE, но она требует держать
        транзакцию открытой дольше.

        ПОЧЕМУ RETURNING, А НЕ rowcount
        --------------------------------
        Сначала успех определялся по `cursor.rowcount == 1`. В одном
        потоке это работает, а под нагрузкой ломается: тест на 20 потоков
        показал, что база отработала верно (остаток ушёл с 1 на 0, ровно
        одна строка изменена), но `rowcount` вернул НОЛЬ всем двадцати
        потокам — включая победителя.

        Причина: `rowcount` берётся из `sqlite3_changes()`, а эта функция
        относится к СОЕДИНЕНИЮ, а не к курсору. Когда несколько потоков
        работают через одно соединение, их вызовы перемежаются, и значение
        успевает перезаписаться чужим запросом до того, как его прочитают.

        Баг был бы катастрофическим: сервис решил бы, что товар
        не зарезервирован, откатил заказ — и списанный остаток вернулся
        бы, хотя покупатель ничего не купил. Или наоборот, при другом
        порядке — заказ прошёл бы дважды.

        `RETURNING` отдаёт строку в результат самого запроса, привязанный
        к своему курсору. Чужой поток на него повлиять не может.
        Поддерживается sqlite с 3.35 и Postgres с 8.2.
        """
        if quantity <= 0:
            raise ValidationError("количество должно быть положительным")

        if self.db.supports_returning:
            row = self.db.query_one(
                """
                UPDATE products
                   SET stock = stock - ?, version = version + 1
                 WHERE id = ? AND stock >= ?
                RETURNING id
                """,
                (quantity, product_id, quantity),
            )
            return row is not None

        # Запасной путь для старых версий sqlite: rowcount читается
        # под блокировкой соединения, чтобы чужой запрос не вклинился.
        with self.db.connection_lock():
            cursor = self.db.execute(
                """
                UPDATE products
                   SET stock = stock - ?, version = version + 1
                 WHERE id = ? AND stock >= ?
                """,
                (quantity, product_id, quantity),
            )
            return cursor.rowcount == 1

    def release_stock(self, product_id: int, quantity: int) -> None:
        """Возврат остатка при отмене заказа."""
        self.db.execute(
            "UPDATE products SET stock = stock + ?, version = version + 1 WHERE id = ?",
            (quantity, product_id),
        )

    def reserve_stock_unsafe(self, product_id: int, quantity: int) -> bool:
        """НАИВНАЯ версия с гонкой. Оставлена намеренно.

        Нужна, чтобы демонстрация была честной: скрипт показывает, что
        на этой реализации товар реально продаётся дважды, а на атомарной
        нет. Утверждение «так делать нельзя» подкрепляется числом
        проданных сверх остатка единиц, а не словами.

        В рабочем коде не используется нигде, кроме демонстрации и теста.
        """
        row = self.db.query_one("SELECT stock FROM products WHERE id = ?", (product_id,))
        if row is None or row["stock"] < quantity:
            return False
        self.db.execute(
            "UPDATE products SET stock = ? WHERE id = ?",
            (row["stock"] - quantity, product_id),
        )
        return True


@dataclass
class SqliteOrderRepository:
    db: Database

    def create(self, order: Order, request_hash: str | None = None) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO orders (customer_id, status, created_at, idempotency_key, request_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                order.customer_id,
                order.status.value,
                order.created_at.isoformat(),
                order.idempotency_key,
                request_hash,
            ),
        )
        order_id = cursor.lastrowid
        # executemany вместо цикла с execute: один вызов драйвера
        # на все строки заказа вместо N.
        self.db.counter.record("INSERT INTO order_lines ... (executemany)")
        self.db.conn.executemany(
            """
            INSERT INTO order_lines (order_id, product_id, sku, quantity, price_kopecks)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (order_id, line.product_id, line.sku, line.quantity, line.price_kopecks)
                for line in order.lines
            ],
        )
        return order_id

    def get(self, order_id: int) -> Order:
        row = self.db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
        if row is None:
            raise NotFound("заказ не найден", order_id=order_id)
        lines = self._lines_for([order_id]).get(order_id, [])
        return self._row_to_order(row, lines)

    def find_by_idempotency_key(self, key: str) -> tuple[Order, str | None] | None:
        row = self.db.query_one(
            "SELECT * FROM orders WHERE idempotency_key = ?", (key,)
        )
        if row is None:
            return None
        lines = self._lines_for([row["id"]]).get(row["id"], [])
        return self._row_to_order(row, lines), row["request_hash"]

    def list_for_customer(self, customer_id: int, limit: int = 50) -> list[Order]:
        """Заказы клиента вместе со строками — ровно ДВА запроса.

        Наивная реализация делает 1 запрос за заказами и ещё по одному
        за строками каждого заказа: 1 + N. На 50 заказах это 51 поход
        в базу, каждый со своим сетевым круговым временем. Здесь строки
        всех заказов берутся одним запросом с IN и раскладываются
        по заказам в памяти.

        Проверяется тестом на счётчике запросов, а не на глаз.
        """
        rows = self.db.query_all(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC LIMIT ?",
            (customer_id, limit),
        )
        if not rows:
            return []
        lines_by_order = self._lines_for([r["id"] for r in rows])
        return [self._row_to_order(r, lines_by_order.get(r["id"], [])) for r in rows]

    def list_for_customer_nplus1(self, customer_id: int, limit: int = 50) -> list[Order]:
        """Наивная версия с N+1. Только для демонстрации и теста."""
        rows = self.db.query_all(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC LIMIT ?",
            (customer_id, limit),
        )
        orders = []
        for row in rows:
            lines = self._lines_for([row["id"]]).get(row["id"], [])
            orders.append(self._row_to_order(row, lines))
        return orders

    def get_full(self, order_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
        if row is None:
            raise NotFound("заказ не найден", order_id=order_id)
        lines = self.db.query_all(
            "SELECT * FROM order_lines WHERE order_id = ? ORDER BY id", (order_id,)
        )
        payload = dict(row)
        payload["lines"] = [dict(line) for line in lines]
        payload["total_kopecks"] = sum(
            line["quantity"] * line["price_kopecks"] for line in lines
        )
        return payload

    def recent(self, limit: int = 20) -> list[dict]:
        """Последние заказы для витрины.

        Суммы считаются агрегатом в базе, а не в Python: тащить все строки
        заказов ради суммы — это и лишний трафик, и лишняя память.
        """
        rows = self.db.query_all(
            """
            SELECT o.id, o.status, o.created_at,
                   COALESCE(SUM(l.quantity * l.price_kopecks), 0) AS total_kopecks,
                   COALESCE(SUM(l.quantity), 0) AS items
              FROM orders o
              LEFT JOIN order_lines l ON l.order_id = o.id
             GROUP BY o.id
             ORDER BY o.id DESC
             LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    def set_status(self, order_id: int, status: OrderStatus) -> None:
        self.db.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (status.value, order_id)
        )

    # ---------- служебное ----------

    def _lines_for(self, order_ids: list[int]) -> dict[int, list[OrderLine]]:
        if not order_ids:
            return {}
        placeholders = ",".join("?" * len(order_ids))
        rows = self.db.query_all(
            f"SELECT * FROM order_lines WHERE order_id IN ({placeholders}) ORDER BY id",
            tuple(order_ids),
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
    def _row_to_order(row, lines: list[OrderLine]) -> Order:
        from datetime import datetime

        return Order(
            id=row["id"],
            customer_id=row["customer_id"],
            lines=lines,
            status=OrderStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            idempotency_key=row["idempotency_key"],
        )
