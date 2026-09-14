"""Отзывы: два репозитория, один интерфейс.

Оба класса лежат в одном файле намеренно. Товары и заказы разнесены
по файлам, потому что там много кода; здесь его мало, а рядом видно
главное: чем именно отличаются реализации. Разница сводится
к плейсхолдерам (`?` против `%s`), способу вернуть вставленную строку
и типу даты — всё остальное совпадает слово в слово.

Про демонстрационные отзывы. Они помечены флагом `is_demo`, и ручка
витрины отдаёт его наружу. Это не формальность: отзыв, выдуманный
для демонстрации, не должен выглядеть как мнение настоящего покупателя
ни в интерфейсе, ни в выгрузке из базы. Убрать их можно одним запросом
по этому же флагу.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..errors import NotFound, ValidationError

MAX_AUTHOR = 80
MAX_TITLE = 120
MAX_BODY = 4000


def validate(author: str, rating: int, body: str, title: str = "") -> None:
    """Проверки ДО обращения к базе.

    Ограничения продублированы в схеме (CHECK), и это не избыточность:
    база — последний рубеж и защищает данные от любого клиента, а код
    даёт человеку внятное сообщение вместо `violates check constraint`.
    """
    problems = []
    if not author.strip():
        problems.append("имя не заполнено")
    elif len(author) > MAX_AUTHOR:
        problems.append(f"имя длиннее {MAX_AUTHOR} символов")
    if not isinstance(rating, int) or isinstance(rating, bool):
        problems.append("оценка должна быть целым числом")
    elif not 1 <= rating <= 5:
        problems.append("оценка должна быть от 1 до 5")
    if not body.strip():
        problems.append("текст отзыва не заполнен")
    elif len(body) > MAX_BODY:
        problems.append(f"текст длиннее {MAX_BODY} символов")
    if len(title) > MAX_TITLE:
        problems.append(f"заголовок длиннее {MAX_TITLE} символов")
    if problems:
        raise ValidationError("отзыв не принят: " + ", ".join(problems))


@dataclass
class SqliteReviewRepository:
    db: object

    def add(self, author: str, rating: int, body: str, title: str = "",
            product_id: int | None = None, is_demo: bool = False,
            created_at: datetime | None = None,
            customer_id: int | None = None, verified: bool = False) -> int:
        validate(author, rating, body, title)
        cursor = self.db.execute(
            """
            INSERT INTO reviews (product_id, author, rating, title, body,
                                 created_at, is_demo, customer_id, verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (product_id, author.strip(), rating, title.strip(), body.strip(),
             (created_at or datetime.now(timezone.utc)).isoformat(), int(is_demo),
             customer_id, int(verified)),
        )
        return cursor.lastrowid

    def list_recent(self, limit: int = 50, product_id: int | None = None) -> list[dict]:
        """Отзывы с названием товара — ОДНИМ запросом.

        Соблазн взять отзывы, а потом названия по одному в цикле — это
        та же проблема N+1, что и у заказов: пятьдесят отзывов
        превращаются в пятьдесят один запрос. LEFT JOIN, а не INNER:
        у отзыва о магазине товара нет, и INNER молча выбросил бы его.
        """
        where = "WHERE r.product_id = ?" if product_id else ""
        params = (product_id, limit) if product_id else (limit,)
        rows = self.db.query_all(
            f"""
            SELECT r.id, r.author, r.rating, r.title, r.body, r.created_at,
                   r.is_demo, r.verified, r.product_id, r.customer_id,
                   p.title AS product_title, p.sku AS product_sku,
                   c.city  AS customer_city
              FROM reviews r
              LEFT JOIN products  p ON p.id = r.product_id
              LEFT JOIN customers c ON c.id = r.customer_id
              {where}
             ORDER BY r.created_at DESC, r.id DESC
             LIMIT ?
            """,
            params,
        )
        return [dict(r) for r in rows]

    def summary(self, product_id: int | None = None) -> dict:
        """Средняя оценка и раскладка по звёздам — одним проходом.

        Пять отдельных COUNT(*) прочитали бы таблицу пять раз. Приём
        со SUM(CASE ...) считает всё за один скан и работает одинаково
        в любой базе.
        """
        where = "WHERE product_id = ?" if product_id else ""
        params = (product_id,) if product_id else ()
        row = self.db.query_one(
            f"""
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(rating), 0) AS average,
                   SUM(CASE WHEN rating = 5 THEN 1 ELSE 0 END) AS stars5,
                   SUM(CASE WHEN rating = 4 THEN 1 ELSE 0 END) AS stars4,
                   SUM(CASE WHEN rating = 3 THEN 1 ELSE 0 END) AS stars3,
                   SUM(CASE WHEN rating = 2 THEN 1 ELSE 0 END) AS stars2,
                   SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS stars1
              FROM reviews
              {where}
            """,
            params,
        )
        return dict(row) if row else {}

    def ratings_by_product(self, product_ids: list[int] | None = None) -> dict[int, dict]:
        """Рейтинги СРАЗУ ПО ВСЕМ товарам — один запрос на весь каталог.

        Это то место, где N+1 возникает само собой: на карточке товара
        хочется показать звёзды, и напрашивается вызвать `summary(id)`
        в цикле отрисовки. Сорок три товара — сорок три запроса,
        а на тысяче позиций страница просто не откроется.

        Здесь база группирует всё за один проход и отдаёт словарь,
        из которого витрина берёт значения по ключу.
        """
        where = ""
        params: tuple = ()
        if product_ids is not None:
            if not product_ids:
                return {}
            placeholders = ", ".join("?" for _ in product_ids)
            where = f"AND product_id IN ({placeholders})"
            params = tuple(product_ids)

        rows = self.db.query_all(
            f"""
            SELECT product_id,
                   COUNT(*) AS total,
                   AVG(rating) AS average
              FROM reviews
             WHERE product_id IS NOT NULL {where}
             GROUP BY product_id
            """,
            params,
        )
        return {
            row["product_id"]: {"total": int(row["total"]),
                                "average": round(float(row["average"]), 2)}
            for row in rows
        }

    def reviewed_product_ids(self, customer_id: int) -> set[int]:
        """На какие товары покупатель уже написал отзыв.

        Отдельный запрос, а не фильтрация общего списка. Первая версия
        брала `list_recent(limit=200)` и отбирала свои строки — и это
        работало ровно до тех пор, пока отзывов было меньше двухсот.
        На шестистах старые отзывы покупателя в окно не попадали,
        профиль предлагал оценить уже оценённое, а сервер отвечал 409.

        Признак таких ошибок общий: выборка «последние N» использована
        как «все». Ограничение сделано для показа, а не для логики.
        """
        rows = self.db.query_all(
            """
            SELECT product_id FROM reviews
             WHERE customer_id = ? AND product_id IS NOT NULL
            """,
            (customer_id,),
        )
        return {row["product_id"] for row in rows}

    def get(self, review_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if row is None:
            raise NotFound("отзыв не найден", review_id=review_id)
        return dict(row)

    def delete_demo(self) -> None:
        self.db.execute("DELETE FROM reviews WHERE is_demo = 1")


@dataclass
class PgReviewRepository:
    db: object

    def add(self, author: str, rating: int, body: str, title: str = "",
            product_id: int | None = None, is_demo: bool = False,
            created_at: datetime | None = None,
            customer_id: int | None = None, verified: bool = False) -> int:
        validate(author, rating, body, title)
        # COALESCE, а не подстановка now() в Python: значение по умолчанию
        # должна ставить база. Часы приложения и часы сервера базы
        # расходятся, и «время создания» лучше брать из одного источника.
        row = self.db.query_one(
            """
            INSERT INTO reviews (product_id, author, rating, title, body,
                                 is_demo, created_at, customer_id, verified)
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s, %s)
            RETURNING id
            """,
            (product_id, author.strip(), rating, title.strip(), body.strip(),
             is_demo, created_at, customer_id, verified),
        )
        return row["id"]

    def list_recent(self, limit: int = 50, product_id: int | None = None) -> list[dict]:
        where = "WHERE r.product_id = %s" if product_id else ""
        params = (product_id, limit) if product_id else (limit,)
        rows = self.db.query_all(
            f"""
            SELECT r.id, r.author, r.rating, r.title, r.body, r.created_at,
                   r.is_demo, r.verified, r.product_id, r.customer_id,
                   p.title AS product_title, p.sku AS product_sku,
                   c.city  AS customer_city
              FROM reviews r
              LEFT JOIN products  p ON p.id = r.product_id
              LEFT JOIN customers c ON c.id = r.customer_id
              {where}
             ORDER BY r.created_at DESC, r.id DESC
             LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in rows]

    def summary(self, product_id: int | None = None) -> dict:
        where = "WHERE product_id = %s" if product_id else ""
        params = (product_id,) if product_id else ()
        row = self.db.query_one(
            f"""
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(rating), 0) AS average,
                   COUNT(*) FILTER (WHERE rating = 5) AS stars5,
                   COUNT(*) FILTER (WHERE rating = 4) AS stars4,
                   COUNT(*) FILTER (WHERE rating = 3) AS stars3,
                   COUNT(*) FILTER (WHERE rating = 2) AS stars2,
                   COUNT(*) FILTER (WHERE rating = 1) AS stars1
              FROM reviews
              {where}
            """,
            params,
        )
        # FILTER — это то же самое, что SUM(CASE ...), но читается лучше
        # и работает быстрее: агрегат считается без разбора выражения
        # на каждой строке. В sqlite его нет, поэтому там CASE.
        return dict(row) if row else {}

    def ratings_by_product(self, product_ids: list[int] | None = None) -> dict[int, dict]:
        """То же самое, но `= ANY(%s)`: список уходит одним параметром.

        План запроса при этом не зависит от длины списка, и база может
        его переиспользовать — в отличие от `IN (...)` со склеенными
        плейсхолдерами, где каждый новый размер списка даёт новый запрос.
        """
        where = ""
        params: tuple = ()
        if product_ids is not None:
            if not product_ids:
                return {}
            where = "AND product_id = ANY(%s)"
            params = (product_ids,)

        rows = self.db.query_all(
            f"""
            SELECT product_id,
                   COUNT(*) AS total,
                   AVG(rating) AS average
              FROM reviews
             WHERE product_id IS NOT NULL {where}
             GROUP BY product_id
            """,
            params,
        )
        return {
            row["product_id"]: {"total": int(row["total"]),
                                "average": round(float(row["average"]), 2)}
            for row in rows
        }

    def reviewed_product_ids(self, customer_id: int) -> set[int]:
        """На какие товары покупатель уже написал отзыв."""
        rows = self.db.query_all(
            """
            SELECT product_id FROM reviews
             WHERE customer_id = %s AND product_id IS NOT NULL
            """,
            (customer_id,),
        )
        return {row["product_id"] for row in rows}

    def get(self, review_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM reviews WHERE id = %s", (review_id,))
        if row is None:
            raise NotFound("отзыв не найден", review_id=review_id)
        return dict(row)

    def delete_demo(self) -> None:
        self.db.execute("DELETE FROM reviews WHERE is_demo = true")
