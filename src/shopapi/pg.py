"""PostgreSQL: пул соединений, транзакции, счётчик запросов.

Отличия от sqlite-версии, которые и составляют суть перехода на Postgres:

* **Пул соединений вместо соединения на поток.** Открытие соединения
  к Postgres — это TCP-рукопожатие, аутентификация и запуск отдельного
  процесса на сервере, то есть десятки миллисекунд. Открывать его
  на каждый запрос нельзя. Пул держит готовые соединения и выдаёт их
  по требованию.

* **Плейсхолдеры `%s` вместо `?`.** Это разница драйверов, а не SQL.
  Значения всё так же передаются параметрами — подставлять их в текст
  запроса нельзя ни в одной базе, это прямая дорога к инъекции.

* **Уровни изоляции задаются явно.** В Postgres по умолчанию
  READ COMMITTED; для сценариев, где нужно больше, уровень указывается
  при старте транзакции.

* **`rowcount` работает корректно.** В sqlite он брался из
  `sqlite3_changes()` — функции уровня соединения, и при общем соединении
  врал. В psycopg `rowcount` принадлежит курсору, а курсор — своему
  соединению из пула. Но `RETURNING` всё равно оставлен: он выразительнее
  и возвращает сами данные, а не только их количество.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_DSN = "postgresql://shop:shop@localhost:5432/shop"


def dsn_from_env(explicit: str | None = None) -> str:
    """Строка подключения: аргумент -> переменная окружения -> умолчание.

    Пароли в коде не держим — только в окружении. Это не паранойя:
    репозиторий рано или поздно окажется в общем доступе, а строка
    подключения остаётся в истории git навсегда.
    """
    return explicit or os.environ.get("DATABASE_URL") or DEFAULT_DSN


def is_available(dsn: str | None = None, timeout: float = 2.0) -> bool:
    """Проверка доступности базы без падения.

    Нужна, чтобы тесты и CLI могли честно сказать «Postgres не поднят»,
    а не вывалить трассировку на пятнадцать строк.
    """
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(dsn_from_env(dsn), connect_timeout=int(timeout)) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 - любая проблема означает «недоступна»
        return False


@dataclass
class QueryCounter:
    """Тот же счётчик, что и в sqlite-версии.

    Он нужен именно на Postgres даже больше: там каждый лишний запрос —
    это ещё и сетевой круг, а не обращение к файлу рядом.
    """

    count: int = 0
    statements: list[str] = field(default_factory=list)
    enabled: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def record(self, sql: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.count += 1
            self.statements.append(" ".join(sql.split())[:90])

    def reset(self) -> None:
        with self._lock:
            self.count = 0
            self.statements.clear()

    @contextmanager
    def measure(self) -> Iterator[QueryCounter]:
        self.reset()
        yield self


class PostgresDatabase:
    """Обёртка над пулом соединений psycopg3."""

    def __init__(self, dsn: str | None = None, min_size: int = 1, max_size: int = 10):
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.dsn = dsn_from_env(dsn)
        self.counter = QueryCounter()
        self._local = threading.local()

        # `open=True` создаёт соединения сразу: иначе первый же запрос
        # оплатит установку соединения своей латенси, и это попадёт
        # в p99 прогретого сервиса.
        self._pool = ConnectionPool(
            self.dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            timeout=10.0,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        self._pool.wait(timeout=10.0)
        logger.info("Пул соединений к Postgres открыт: %s", self._safe_dsn())

    def _safe_dsn(self) -> str:
        """DSN без пароля — чтобы он не утёк в логи."""
        dsn = self.dsn
        if "@" in dsn and "://" in dsn:
            scheme, rest = dsn.split("://", 1)
            creds, host = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
        return dsn

    # ---------- соединения ----------

    @contextmanager
    def connection(self) -> Iterator:
        """Соединение из пула, либо текущее транзакционное.

        Внутри `transaction()` все запросы обязаны идти через ОДНО
        соединение — иначе они окажутся в разных транзакциях, и откат
        не откатит половину работы. Поэтому активное соединение хранится
        в `threading.local`.
        """
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            yield existing
            return
        with self._pool.connection() as conn:
            yield conn

    @property
    def supports_returning(self) -> bool:
        return True

    # ---------- выполнение ----------

    def execute(self, sql: str, params: tuple = ()):
        self.counter.record(sql)
        with self.connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor

    def query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        self.counter.record(sql)
        with self.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        self.counter.record(sql)
        with self.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.counter.record(sql + " (executemany)")
        with self.connection() as conn:
            conn.cursor().executemany(sql, rows)

    # ---------- транзакции ----------

    @contextmanager
    def transaction(self, isolation: str | None = None) -> Iterator:
        """Транзакция на одном соединении из пула.

        `isolation` позволяет поднять уровень для конкретной операции:

        * READ COMMITTED (по умолчанию) — видим только подтверждённое;
        * REPEATABLE READ — повторное чтение в транзакции даёт то же;
        * SERIALIZABLE — как будто транзакции выполнялись по очереди.

        Чем строже уровень, тем больше конфликтов сериализации, которые
        надо ловить и повторять. Поэтому уровень поднимают точечно, а не
        глобально «на всякий случай».
        """
        if getattr(self._local, "conn", None) is not None:
            # Вложенная транзакция: Postgres умеет SAVEPOINT, но здесь
            # достаточно переиспользовать внешнюю — вложенность в этом
            # проекте означает «сервис вызвал сервис», и отдельная точка
            # отката там не нужна.
            yield self._local.conn
            return

        with self._pool.connection() as conn:
            conn.autocommit = False
            if isolation:
                conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
            self._local.conn = conn
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
            finally:
                self._local.conn = None
                conn.autocommit = True

    # ---------- служебное ----------

    def explain(self, sql: str, params: tuple = (), analyze: bool = True) -> str:
        """План выполнения запроса.

        Оптимизировать запрос, не глядя в план, — гадание. `ANALYZE`
        реально выполняет запрос и показывает фактическое время и число
        строк, а не только оценку планировщика; расхождение оценки
        и факта само по себе диагноз — обычно устаревшая статистика.
        """
        prefix = "EXPLAIN (ANALYZE, BUFFERS)" if analyze else "EXPLAIN"
        with self.connection() as conn:
            rows = conn.execute(f"{prefix} {sql}", params).fetchall()
        return "\n".join(row["QUERY PLAN"] for row in rows)

    def close(self) -> None:
        self._pool.close()
        logger.info("Пул соединений закрыт")
