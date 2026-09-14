"""Работа с SQLite: схема, соединения, транзакции, счётчик запросов.

Почему sqlite и стандартная библиотека, а не SQLAlchemy с Postgres:
проект должен запускаться командой `python main.py demo` без единой
внешней зависимости. Всё, что здесь показано — транзакции, уровни
изоляции, атомарные UPDATE, индексы, keyset-пагинация — переносится
на Postgres один в один, меняется только диалект.

Счётчик запросов встроен намеренно: без него проблему N+1 нельзя
ни увидеть, ни доказать. «Кажется, стало быстрее» — не аргумент,
«было 51 запрос, стало 2» — аргумент.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sku           TEXT    NOT NULL UNIQUE,
    title         TEXT    NOT NULL,
    -- Поля витрины держатся в обеих схемах одинаковыми (в PostgreSQL их
    -- добавляет миграция 002). Иначе одно и то же приложение работало бы
    -- на одной базе и падало на другой — именно так и вышло в первой
    -- версии: витрина жила только на Postgres.
    description   TEXT    NOT NULL DEFAULT '',
    category      TEXT    NOT NULL DEFAULT 'Прочее',
    emoji         TEXT    NOT NULL DEFAULT '📦',
    platform      TEXT    NOT NULL DEFAULT '',
    genre         TEXT    NOT NULL DEFAULT '',
    developer     TEXT    NOT NULL DEFAULT '',
    year          INTEGER NOT NULL DEFAULT 0,
    -- Характеристики одной колонкой в JSON: у консоли, игры и накопителя
    -- наборы полей не пересекаются, а искать по ним проект не умеет —
    -- они только показываются на карточке. Таблица «атрибут — значение»
    -- дала бы join и сортировку там, где хватает одного поля.
    specs         TEXT    NOT NULL DEFAULT '{}',
    price_kopecks INTEGER NOT NULL CHECK (price_kopecks >= 0),
    stock         INTEGER NOT NULL CHECK (stock >= 0),
    version       INTEGER NOT NULL DEFAULT 1
);

-- Индекс по категории создаётся НЕ здесь, а после досоздания колонок
-- (см. _upgrade_products): на файле от прошлой версии колонки ещё нет,
-- и CREATE INDEX упал бы раньше, чем ALTER успел её добавить.

-- Индекс под keyset-пагинацию каталога: сортировка идёт по (title, id),
-- и составной индекс позволяет базе отдать страницу без сортировки всей
-- таблицы. Без него запрос на глубокой странице читает весь каталог.
CREATE INDEX IF NOT EXISTS idx_products_title_id ON products (title, id);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     INTEGER NOT NULL,
    status          TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    idempotency_key TEXT,
    request_hash    TEXT
);

-- Уникальность ключа идемпотентности обеспечивается БАЗОЙ, а не кодом.
-- Проверка «сначала SELECT, потом INSERT» в коде — это гонка: два
-- параллельных запроса оба увидят пустой результат и оба вставят заказ.
-- Здесь второй INSERT упадёт на ограничении, и это правильное поведение.
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency
    ON orders (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders (customer_id, id);

CREATE TABLE IF NOT EXISTS order_lines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id    INTEGER NOT NULL REFERENCES products (id),
    sku           TEXT    NOT NULL,
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    price_kopecks INTEGER NOT NULL
);

-- Без этого индекса выборка строк заказа делает полный перебор,
-- и «загрузить 50 заказов со строками» превращается в 50 сканов.
CREATE INDEX IF NOT EXISTS idx_order_lines_order ON order_lines (order_id);
"""

CUSTOMERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    email     TEXT NOT NULL UNIQUE,
    city      TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL,
    is_demo   INTEGER NOT NULL DEFAULT 0,
    -- Строка вида `scrypt$соль$хеш`. NULL — профиль без пароля:
    -- под демонстрационными покупателями войти нельзя.
    password_hash TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- В базе хеш токена, а не токен: утечка таблицы не даёт войти.
    token_hash  TEXT    NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_customer ON sessions (customer_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);
"""

# Отзывы вынесены отдельным куском: таблица ссылается на products,
# поэтому создаётся ПОСЛЕ того, как товарам досоздали колонки.
REVIEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER          REFERENCES products (id) ON DELETE SET NULL,
    author      TEXT    NOT NULL,
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    title       TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    is_demo     INTEGER NOT NULL DEFAULT 0
);

-- ON DELETE SET NULL, а не CASCADE: отзыв о снятом с продажи товаре —
-- это всё ещё мнение покупателя о магазине, и терять его вместе
-- с позицией каталога неправильно. Поэтому product_id и NULLable.

CREATE INDEX IF NOT EXISTS idx_reviews_product ON reviews (product_id, id);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews (created_at DESC);
"""


@dataclass
class QueryCounter:
    """Считает запросы к базе — инструмент против N+1.

    Проблема N+1 незаметна на десяти записях в тесте и убивает сервис
    на тысяче в проде. Счётчик делает её измеримой: тест может прямо
    утверждать «на загрузку N заказов уходит не больше 2 запросов»,
    и такой тест сломается, когда кто-то добавит обращение к базе
    внутрь цикла.
    """

    count: int = 0
    statements: list[str] = field(default_factory=list)
    enabled: bool = True

    def record(self, sql: str) -> None:
        if not self.enabled:
            return
        self.count += 1
        # Храним только начало запроса: полные тексты раздувают вывод,
        # а для диагностики хватает видеть форму запроса.
        self.statements.append(" ".join(sql.split())[:90])

    def reset(self) -> None:
        self.count = 0
        self.statements.clear()

    @contextmanager
    def measure(self) -> Iterator[QueryCounter]:
        self.reset()
        yield self


class Database:
    """Тонкая обёртка над sqlite3.

    Что здесь важно и переносится на любую базу:

    * соединение на поток. sqlite3-соединение нельзя шарить между
      потоками, а в веб-сервисе потоков несколько. Отсюда
      threading.local — примитивный аналог пула соединений;

    * `isolation_level=None` отключает неявные транзакции драйвера
      и возвращает контроль коду: транзакция начинается там, где
      написано BEGIN, а не там, где драйвер решил;

    * WAL-режим: читатели не блокируют писателя. Без него параллельные
      чтения и записи упираются в блокировку всей базы;

    * `busy_timeout` — ждать освобождения блокировки, а не падать сразу.
    """

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.counter = QueryCounter()
        self._local = threading.local()
        self._temp_path: str | None = None
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()

        # Режим ":memory:" — одно общее соединение, потому что у каждого
        # соединения к ":memory:" СВОЯ отдельная база. Годится только для
        # однопоточной работы: см. комментарий к `temporary()`.
        self._shared: sqlite3.Connection | None = None
        self._lock: threading.RLock | None = None  # type: ignore[assignment]
        if path == ":memory:":
            self._shared = self._connect()
            # RLock, а не Lock: `transaction()` берёт блокировку и внутри
            # вызывает `execute()`, который берёт её же. С обычным Lock
            # это мгновенный самоблок.
            self._lock = threading.RLock()

    @classmethod
    def temporary(cls) -> Database:
        """База во временном файле — то, что нужно для многопоточности.

        Почему не ":memory:". У каждого соединения к ":memory:" своя
        отдельная база, поэтому потокам пришлось бы делить ОДНО
        соединение. А одно соединение sqlite3, используемое несколькими
        потоками одновременно, ломается: на Python 3.14 под Windows это
        падает с `sqlite3.InterfaceError: bad parameter or other API
        misuse`, на других версиях может тихо возвращать мусор.

        Это тот же класс проблем, что и найденный в `reserve_stock`
        (см. там про `rowcount`): состояние соединения общее, а потоки
        об этом не знают.

        Правильное решение — то же, что в любом настоящем сервисе:
        **по соединению на поток**, а база одна и общая. Для этого
        нужна база в файле. Временный файл удаляется в `close()`.
        """
        # mkstemp вместо NamedTemporaryFile: нужен только путь, файл
        # откроет и закроет сам sqlite. Дескриптор закрываем сразу,
        # иначе на Windows он будет держать файл.
        descriptor, path = tempfile.mkstemp(suffix=".sqlite3")
        import os

        os.close(descriptor)
        db = cls(path)
        db._temp_path = path
        return db

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row

        # Встроенные LOWER() и LIKE в sqlite работают только с латиницей:
        # 'Консоль' остаётся 'Консоль', и поиск по русскому каталогу
        # становится регистрозависимым. (Сборка с ICU это чинит, но
        # рассчитывать на неё нельзя — её нет в стандартном Python.)
        # Регистрируем свою функцию: Python знает Unicode целиком.
        # В PostgreSQL проблемы нет вовсе, там ILIKE знает про регистр
        # по правилам локали.
        conn.create_function("pylower", 1, lambda s: s.lower() if s else s,
                             deterministic=True)

        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            # WAL: читатели не блокируют писателя. Без него параллельные
            # чтения и записи упираются в блокировку всей базы.
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        if not hasattr(self._local, "conn"):
            connection = self._connect()
            self._local.conn = connection
            # Список нужен, чтобы закрыть соединения чужих потоков
            # в `close()`: сам поток к тому времени уже завершится,
            # а файл останется занятым.
            with self._connections_lock:
                self._connections.append(connection)
        return self._local.conn

    @property
    def supports_returning(self) -> bool:
        """Доступен ли `UPDATE ... RETURNING` (sqlite 3.35+).

        Нужен потому, что `cursor.rowcount` нельзя использовать для
        определения успеха, когда через одно соединение работают
        несколько потоков: значение берётся из `sqlite3_changes()`,
        а эта функция относится к соединению, и чужой запрос успевает
        его перезаписать. Подробности — в докстринге `reserve_stock`.
        """
        return sqlite3.sqlite_version_info >= (3, 35, 0)

    @contextmanager
    def connection_lock(self) -> Iterator[None]:
        """Блокировка соединения — запасной путь для старых sqlite."""
        if self._lock is not None:
            self._lock.acquire()
        try:
            yield
        finally:
            if self._lock is not None:
                self._lock.release()

    # Колонки витрины, появившиеся позже первой схемы. В PostgreSQL их
    # приносит миграция 002; здесь роль миграции играет этот список.
    PRODUCT_COLUMNS_ADDED_LATER = (
        ("description", "TEXT NOT NULL DEFAULT ''"),
        ("category", "TEXT NOT NULL DEFAULT 'Прочее'"),
        ("emoji", "TEXT NOT NULL DEFAULT '📦'"),
        ("platform", "TEXT NOT NULL DEFAULT ''"),
        ("genre", "TEXT NOT NULL DEFAULT ''"),
        ("developer", "TEXT NOT NULL DEFAULT ''"),
        ("year", "INTEGER NOT NULL DEFAULT 0"),
        ("specs", "TEXT NOT NULL DEFAULT '{}'"),
    )

    # То же для отзывов: таблица появилась раньше, чем покупатели.
    REVIEW_COLUMNS_ADDED_LATER = (
        ("customer_id", "INTEGER REFERENCES customers (id) ON DELETE SET NULL"),
        ("verified", "INTEGER NOT NULL DEFAULT 0"),
    )

    CUSTOMER_COLUMNS_ADDED_LATER = (
        ("password_hash", "TEXT"),
    )

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._upgrade_products()

    def _upgrade_products(self) -> None:
        """Досоздаёт колонки в УЖЕ существующем файле базы.

        `CREATE TABLE IF NOT EXISTS` не трогает таблицу, которая уже есть:
        файл, созданный прошлой версией, так и остался бы без колонок
        витрины, и приложение падало бы на `no such column`. Причём
        не на старте, а при первом запросе каталога — то есть у
        пользователя, а не у разработчика.

        Это ровно та задача, ради которой существуют миграции. Здесь
        хватает трёх ALTER'ов: `ADD COLUMN` в sqlite не переписывает
        таблицу и не блокирует её надолго, а `PRAGMA table_info`
        показывает, чего не хватает, — так что операция идемпотентна
        и безопасна на любой из версий файла.
        """
        existing = {row["name"] for row in
                    self.conn.execute("PRAGMA table_info(products)").fetchall()}
        for name, definition in self.PRODUCT_COLUMNS_ADDED_LATER:
            if name not in existing:
                self.conn.execute(
                    f"ALTER TABLE products ADD COLUMN {name} {definition}"
                )

        # Теперь колонки точно есть — можно вешать индексы.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_category ON products (category)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_platform ON products (platform)"
        )
        self.conn.executescript(CUSTOMERS_SCHEMA)
        self._upgrade_customers()
        self.conn.executescript(REVIEWS_SCHEMA)
        self._upgrade_reviews()

    def _upgrade_customers(self) -> None:
        """Досоздаёт колонки покупателей в уже существующем файле."""
        existing = {row["name"] for row in
                    self.conn.execute("PRAGMA table_info(customers)").fetchall()}
        for name, definition in self.CUSTOMER_COLUMNS_ADDED_LATER:
            if name not in existing:
                self.conn.execute(f"ALTER TABLE customers ADD COLUMN {name} {definition}")

    def _upgrade_reviews(self) -> None:
        """Досоздаёт колонки отзывов в уже существующем файле базы."""
        existing = {row["name"] for row in
                    self.conn.execute("PRAGMA table_info(reviews)").fetchall()}
        for name, definition in self.REVIEW_COLUMNS_ADDED_LATER:
            if name not in existing:
                self.conn.execute(f"ALTER TABLE reviews ADD COLUMN {name} {definition}")

        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reviews_customer ON reviews (customer_id)"
        )
        # Один отзыв от покупателя на товар — ограничением в базе,
        # а не проверкой в коде: кнопку на витрине можно спрятать,
        # но запрос никто не мешает послать напрямую.
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_one_per_customer
                ON reviews (customer_id, product_id)
             WHERE customer_id IS NOT NULL AND product_id IS NOT NULL
            """
        )

    # ---------- выполнение ----------

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        self.counter.record(sql)
        if self._lock is not None:
            # Режим ":memory:" делит одно соединение между потоками.
            # Одновременные вызовы на нём — это порча внутреннего
            # состояния драйвера (на Python 3.14 сразу InterfaceError),
            # поэтому здесь они выстраиваются в очередь.
            # Для настоящей параллельной работы нужен `Database.temporary()`
            # с соединением на поток.
            with self._lock:
                return self.conn.execute(sql, params)
        return self.conn.execute(sql, params)

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    # ---------- транзакции ----------

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Транзакция с откатом при любом исключении.

        `BEGIN IMMEDIATE` вместо обычного BEGIN — важная деталь. Обычный
        BEGIN берёт блокировку записи лениво, при первом UPDATE. Если две
        транзакции начались, обе почитали и обе пошли писать, вторая
        получает «database is locked» уже после работы — и её приходится
        откатывать целиком. IMMEDIATE берёт блокировку сразу: конкурент
        ждёт на входе, а не проигрывает на выходе.

        В Postgres роль этого выбора играют уровни изоляции и SELECT FOR
        UPDATE, но рассуждение то же самое.
        """
        if self._lock is not None:
            self._lock.acquire()
        try:
            self.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.conn
            except Exception:
                self.execute("ROLLBACK")
                raise
            else:
                self.execute("COMMIT")
        finally:
            if self._lock is not None:
                self._lock.release()

    def close(self) -> None:
        """Закрывает все соединения и убирает временный файл.

        Соединения чужих потоков закрываются по списку: сами потоки
        к этому моменту уже завершились, а на Windows незакрытое
        соединение держит файл и не даёт его удалить.
        """
        if self._shared is not None:
            self._shared.close()
            self._shared = None

        with self._connections_lock:
            for connection in self._connections:
                with contextlib.suppress(sqlite3.Error):
                    connection.close()
            self._connections.clear()
        if hasattr(self._local, "conn"):
            del self._local.conn

        if self._temp_path:
            import os

            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(OSError):
                    os.unlink(self._temp_path + suffix)
            self._temp_path = None
