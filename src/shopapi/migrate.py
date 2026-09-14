"""Миграции схемы: маленький runner поверх SQL-файлов.

Почему не alembic: он хорош и в рабочем проекте я бы взял его, но здесь
важнее показать механику, а она простая и умещается в сто строк.
Понимание того, что alembic делает под капотом, полезнее умения
вызвать `alembic upgrade head`.

Механика любой системы миграций:

1. файлы пронумерованы и применяются строго по порядку;
2. таблица в самой базе помнит, что уже применено;
3. применение идёт в транзакции — миграция либо прошла целиком,
   либо не прошла вовсе;
4. применённые файлы не правят, добавляют новые.

Пункт 4 — главный: правка уже применённой миграции означает, что
на разных стендах схема разъедется, и никто этого не заметит.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER     PRIMARY KEY,
    name        TEXT        NOT NULL,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_NAME_RE = re.compile(r"^(\d+)[_-](.+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()[:16]


def discover(directory: str | Path) -> list[Migration]:
    """Читает файлы вида `001_initial.sql` и сортирует по номеру.

    Сортировка именно по числу, а не по имени строкой: иначе `010`
    окажется раньше `9`, и миграции применятся не в том порядке.
    """
    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(f"каталог миграций не найден: {directory}")

    found: list[Migration] = []
    for path in directory.iterdir():
        match = _NAME_RE.match(path.name)
        if not match:
            continue
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=path.read_text(encoding="utf-8"),
            )
        )

    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise ValueError(f"повторяющиеся номера миграций: {versions}")
    return found


def applied_versions(db) -> dict[int, str]:
    db.execute(MIGRATIONS_TABLE)
    rows = db.query_all("SELECT version, checksum FROM schema_migrations")
    return {row["version"]: row["checksum"] for row in rows}


def migrate(db, directory: str | Path, verbose: bool = True) -> list[Migration]:
    """Применяет недостающие миграции. Возвращает применённые.

    Каждая миграция идёт в своей транзакции: если третья упала, первые
    две остаются применёнными, и после починки процесс продолжится
    с третьей. Одна транзакция на все миграции звучит безопаснее, но
    на практике мешает — часть операций в Postgres нетранзакционна
    (например, CREATE INDEX CONCURRENTLY).
    """
    migrations = discover(directory)
    already = applied_versions(db)

    # Проверка контрольных сумм: если применённый файл изменили,
    # схема на разных стендах разъехалась. Молчать об этом нельзя.
    for migration in migrations:
        stored = already.get(migration.version)
        if stored and stored != migration.checksum:
            raise ValueError(
                f"миграция {migration.version:03d}_{migration.name} изменена "
                f"после применения (было {stored}, стало {migration.checksum}). "
                "Применённые миграции не правят — добавьте новую."
            )

    pending = [m for m in migrations if m.version not in already]
    if not pending and verbose:
        logger.info("Схема актуальна, миграций к применению нет")

    for migration in pending:
        if verbose:
            logger.info("Применяю %03d_%s", migration.version, migration.name)
        with db.transaction():
            db.execute(migration.sql)
            db.execute(
                "INSERT INTO schema_migrations (version, name, checksum) "
                "VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
    return pending


def status(db, directory: str | Path) -> list[tuple[Migration, bool]]:
    """Что применено, а что нет — для команды `migrate --status`."""
    already = applied_versions(db)
    return [(m, m.version in already) for m in discover(directory)]
