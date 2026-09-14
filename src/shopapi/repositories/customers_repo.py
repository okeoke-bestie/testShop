"""Покупатели: регистрация, вход, профиль, право оставить отзыв.

Главное здесь — метод `purchased_products`. Отзыв на витрине можно
оставить только на купленный товар, и проверять это должен сервер:
кнопку в интерфейсе легко спрятать, но запрос никто не мешает послать
напрямую. Правило, которое держится только на вёрстке, — это не правило.

Вход настоящий: пароль хешируется scrypt, сессия живёт токеном
с ограниченным сроком, в базе лежит хеш токена. Подробности — в `auth.py`.

Отдельно стоит отметить, чего здесь НЕТ и почему.

**Списка всех покупателей наружу больше не отдаётся.** Раньше витрина
показывала все профили и предлагала выбрать любой. Это удобно
для демонстрации и совершенно неприемлемо как поведение: адреса почты
чужих людей — персональные данные, и отдавать их всем подряд нельзя.
Теперь войти можно только в свой профиль, зная пароль.

**Нет ограничения частоты попыток входа.** Это честный пробел:
в рабочем сервисе на ручке входа стоит счётчик неудач по адресу
и по IP, иначе пароль подбирают перебором. Здесь его нет, потому
что он требует общего хранилища счётчиков и разговора про обход
через прокси — тема отдельная.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..auth import (
    check_password_strength,
    hash_password,
    new_token,
    session_expiry,
    token_hash,
    verify_password,
)
from ..errors import BadCredentials, EmailTaken, NotFound, ValidationError

# Заготовленный хеш для несуществующего адреса. Нужен только затем,
# чтобы ответ «нет такого пользователя» занимал столько же времени,
# сколько «неверный пароль»: иначе существующие адреса вычисляются
# секундомером, независимо от текста сообщения.
_DUMMY_HASH = hash_password("dummy-password-for-constant-time")


def _is_unique_violation(exc: Exception) -> bool:
    """Нарушение уникальности — по-разному в двух драйверах."""
    name = type(exc).__name__
    if name in {"UniqueViolation", "IntegrityError"}:
        return "unique" in str(exc).lower() or name == "UniqueViolation"
    return False


def validate(name: str, email: str) -> None:
    problems = []
    if not name.strip():
        problems.append("имя не заполнено")
    if "@" not in email or email.strip().startswith("@"):
        problems.append("почта выглядит некорректной")
    if problems:
        raise ValidationError("профиль не принят: " + ", ".join(problems))


@dataclass
class SqliteCustomerRepository:
    db: object

    def add(self, name: str, email: str, city: str = "",
            is_demo: bool = False, joined_at: datetime | None = None) -> int:
        validate(name, email)
        cursor = self.db.execute(
            """
            INSERT INTO customers (name, email, city, joined_at, is_demo)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name.strip(), email.strip().lower(), city.strip(),
             (joined_at or datetime.now(timezone.utc)).isoformat(), int(is_demo)),
        )
        return cursor.lastrowid

    # ---------- регистрация и вход ----------

    def register(self, name: str, email: str, password: str, city: str = "") -> int:
        """Создаёт профиль с паролем.

        Уникальность почты обеспечивает БАЗА. Проверка «сначала SELECT,
        потом INSERT» — гонка: два одновременных запроса оба увидят,
        что адрес свободен. Поэтому дубль ловится на ограничении
        и переводится в доменную ошибку.
        """
        validate(name, email)
        problems = check_password_strength(password)
        if problems:
            raise ValidationError("пароль не принят: " + ", ".join(problems))

        try:
            cursor = self.db.execute(
                """
                INSERT INTO customers (name, email, city, joined_at, is_demo,
                                       password_hash)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (name.strip(), email.strip().lower(), city.strip(),
                 datetime.now(timezone.utc).isoformat(), hash_password(password)),
            )
        except Exception as exc:  # noqa: BLE001
            if _is_unique_violation(exc):
                raise EmailTaken("этот адрес уже зарегистрирован",
                                 email=email.strip().lower()) from exc
            raise
        return cursor.lastrowid

    def authenticate(self, email: str, password: str) -> dict:
        """Проверяет пару «почта + пароль».

        Сообщение об ошибке ОДНО и то же для «нет такого адреса»
        и «неверный пароль». Разные сообщения превращают форму входа
        в проверялку существующих адресов: подставляя почту, можно
        узнать, кто зарегистрирован.

        Хеш считается даже для несуществующего адреса — иначе ответ
        приходит заметно быстрее, и разницу во времени видно снаружи
        ничуть не хуже разного текста.
        """
        row = self.db.query_one(
            "SELECT * FROM customers WHERE email = ?", (email.strip().lower(),)
        )
        stored = row["password_hash"] if row else None
        if not verify_password(password, stored):
            if row is None:
                verify_password(password, _DUMMY_HASH)
            raise BadCredentials("неверный адрес или пароль")
        return dict(row)

    def start_session(self, customer_id: int) -> str:
        """Заводит сессию и возвращает токен.

        Токен возвращается ОДИН раз и больше нигде не хранится
        в открытом виде: в базе лежит его хеш.
        """
        token = new_token()
        self.db.execute(
            """
            INSERT INTO sessions (token_hash, customer_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token_hash(token), customer_id,
             datetime.now(timezone.utc).isoformat(), session_expiry().isoformat()),
        )
        return token

    def customer_by_token(self, token: str) -> dict | None:
        """Покупатель по токену сессии, если она жива.

        Срок проверяется В ЗАПРОСЕ, а не в Python: иначе просроченная
        сессия считалась бы действующей ровно до того момента, пока
        кто-нибудь не вспомнит проверить дату.
        """
        if not token:
            return None
        row = self.db.query_one(
            """
            SELECT c.* FROM sessions s
              JOIN customers c ON c.id = s.customer_id
             WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash(token), datetime.now(timezone.utc).isoformat()),
        )
        return dict(row) if row else None

    def end_session(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def purge_expired_sessions(self) -> int:
        """Уборка просроченных сессий.

        Без неё таблица растёт вечно: каждая запись живёт после
        истечения срока, ничего не делая. Вызывается на старте —
        для проекта такого размера этого достаточно, в нагруженном
        сервисе это была бы фоновая задача по расписанию.
        """
        cursor = self.db.execute(
            "DELETE FROM sessions WHERE expires_at <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def get(self, customer_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM customers WHERE id = ?", (customer_id,))
        if row is None:
            raise NotFound("покупатель не найден", customer_id=customer_id)
        return dict(row)

    def find_by_email(self, email: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM customers WHERE email = ?", (email.strip().lower(),)
        )
        return dict(row) if row else None

    def list_all(self, limit: int = 100) -> list[dict]:
        """Профили со сводкой по заказам — одним запросом.

        Подзапросы в SELECT здесь дешевле, чем два прохода: список
        коротким не бывает, а собирать счётчики в Python значило бы
        тащить все заказы на клиента ради двух чисел.
        """
        rows = self.db.query_all(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS orders_count,
                   (SELECT COUNT(*) FROM reviews r WHERE r.customer_id = c.id) AS reviews_count
              FROM customers c
             ORDER BY c.id
             LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    def purchased_products(self, customer_id: int) -> list[dict]:
        """Что покупатель купил — и когда в последний раз.

        `DISTINCT` через GROUP BY: один товар мог покупаться несколько
        раз, а для права на отзыв важен сам факт. Отменённые заказы
        исключены — товар вернулся на склад, покупки не было.
        """
        rows = self.db.query_all(
            """
            SELECT l.product_id,
                   p.sku, p.title, p.category, p.platform,
                   MAX(o.created_at) AS last_order_at,
                   SUM(l.quantity)   AS total_quantity,
                   MAX(o.id)         AS last_order_id
              FROM order_lines l
              JOIN orders   o ON o.id = l.order_id
              JOIN products p ON p.id = l.product_id
             WHERE o.customer_id = ? AND o.status <> 'cancelled'
             GROUP BY l.product_id, p.sku, p.title, p.category, p.platform
             ORDER BY MAX(o.id) DESC
            """,
            (customer_id,),
        )
        return [dict(r) for r in rows]

    def has_purchased(self, customer_id: int, product_id: int) -> bool:
        """Проверка права на отзыв — ОДНИМ запросом, а не выборкой всего.

        `LIMIT 1` здесь не украшение: база останавливается на первой
        подходящей строке вместо того, чтобы собирать всю историю
        покупок ради ответа «да/нет».
        """
        row = self.db.query_one(
            """
            SELECT 1 AS found
              FROM order_lines l
              JOIN orders o ON o.id = l.order_id
             WHERE o.customer_id = ? AND l.product_id = ? AND o.status <> 'cancelled'
             LIMIT 1
            """,
            (customer_id, product_id),
        )
        return row is not None

    def delete_demo(self) -> None:
        self.db.execute("DELETE FROM customers WHERE is_demo = 1")


@dataclass
class PgCustomerRepository:
    db: object

    def add(self, name: str, email: str, city: str = "",
            is_demo: bool = False, joined_at: datetime | None = None) -> int:
        validate(name, email)
        row = self.db.query_one(
            """
            INSERT INTO customers (name, email, city, joined_at, is_demo)
            VALUES (%s, %s, %s, COALESCE(%s, now()), %s)
            RETURNING id
            """,
            (name.strip(), email.strip().lower(), city.strip(), joined_at, is_demo),
        )
        return row["id"]

    # ---------- регистрация и вход ----------

    def register(self, name: str, email: str, password: str, city: str = "") -> int:
        validate(name, email)
        problems = check_password_strength(password)
        if problems:
            raise ValidationError("пароль не принят: " + ", ".join(problems))

        try:
            row = self.db.query_one(
                """
                INSERT INTO customers (name, email, city, is_demo, password_hash)
                VALUES (%s, %s, %s, false, %s)
                RETURNING id
                """,
                (name.strip(), email.strip().lower(), city.strip(),
                 hash_password(password)),
            )
        except Exception as exc:  # noqa: BLE001
            if _is_unique_violation(exc):
                raise EmailTaken("этот адрес уже зарегистрирован",
                                 email=email.strip().lower()) from exc
            raise
        return row["id"]

    def authenticate(self, email: str, password: str) -> dict:
        row = self.db.query_one(
            "SELECT * FROM customers WHERE email = %s", (email.strip().lower(),)
        )
        stored = row["password_hash"] if row else None
        if not verify_password(password, stored):
            if row is None:
                verify_password(password, _DUMMY_HASH)
            raise BadCredentials("неверный адрес или пароль")
        return dict(row)

    def start_session(self, customer_id: int) -> str:
        token = new_token()
        self.db.execute(
            """
            INSERT INTO sessions (token_hash, customer_id, expires_at)
            VALUES (%s, %s, %s)
            """,
            (token_hash(token), customer_id, session_expiry()),
        )
        return token

    def customer_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        row = self.db.query_one(
            """
            SELECT c.* FROM sessions s
              JOIN customers c ON c.id = s.customer_id
             WHERE s.token_hash = %s AND s.expires_at > now()
            """,
            (token_hash(token),),
        )
        return dict(row) if row else None

    def end_session(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token),))

    def purge_expired_sessions(self) -> int:
        cursor = self.db.execute("DELETE FROM sessions WHERE expires_at <= now()")
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def get(self, customer_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM customers WHERE id = %s", (customer_id,))
        if row is None:
            raise NotFound("покупатель не найден", customer_id=customer_id)
        return dict(row)

    def find_by_email(self, email: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM customers WHERE email = %s", (email.strip().lower(),)
        )
        return dict(row) if row else None

    def list_all(self, limit: int = 100) -> list[dict]:
        rows = self.db.query_all(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS orders_count,
                   (SELECT COUNT(*) FROM reviews r WHERE r.customer_id = c.id) AS reviews_count
              FROM customers c
             ORDER BY c.id
             LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    def purchased_products(self, customer_id: int) -> list[dict]:
        rows = self.db.query_all(
            """
            SELECT l.product_id,
                   p.sku, p.title, p.category, p.platform,
                   MAX(o.created_at) AS last_order_at,
                   SUM(l.quantity)   AS total_quantity,
                   MAX(o.id)         AS last_order_id
              FROM order_lines l
              JOIN orders   o ON o.id = l.order_id
              JOIN products p ON p.id = l.product_id
             WHERE o.customer_id = %s AND o.status <> 'cancelled'
             GROUP BY l.product_id, p.sku, p.title, p.category, p.platform
             ORDER BY MAX(o.id) DESC
            """,
            (customer_id,),
        )
        return [dict(r) for r in rows]

    def has_purchased(self, customer_id: int, product_id: int) -> bool:
        row = self.db.query_one(
            """
            SELECT 1 AS found
              FROM order_lines l
              JOIN orders o ON o.id = l.order_id
             WHERE o.customer_id = %s AND l.product_id = %s AND o.status <> 'cancelled'
             LIMIT 1
            """,
            (customer_id, product_id),
        )
        return row is not None

    def delete_demo(self) -> None:
        self.db.execute("DELETE FROM customers WHERE is_demo = true")
