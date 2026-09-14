"""Сборка бэкенда: sqlite или PostgreSQL по одному переключателю.

Ради этого и разделены слои. Сервисы, доменные модели и вся бизнес-логика
не знают, какая база под ними: они работают с репозиторием через его
интерфейс. Смена базы — это выбор реализации здесь, и больше нигде.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@dataclass
class Backend:
    """Всё, что нужно приложению: база, репозитории, сервисы."""

    db: object
    products: object
    orders: object
    reviews: object
    customers: object
    order_service: object
    catalog_service: object
    kind: str

    def close(self) -> None:
        self.db.close()


def make_backend(
    dsn: str | None = None,
    sqlite_path: str | None = None,
    migrate_schema: bool = True,
) -> Backend:
    """Собирает бэкенд.

    Выбор базы: явный DSN -> переменная DATABASE_URL -> sqlite.
    Такой порядок удобен на практике: в разработке ничего настраивать
    не надо, в проде достаточно выставить одну переменную окружения.
    """
    from .env_file import load as load_env
    from .services.catalog import CatalogService
    from .services.orders import OrderService

    # .env читается здесь, а не только в main.py: uvicorn запускает
    # приложение отдельным процессом, и тот про наш разбор аргументов
    # ничего не знает. Настоящее окружение при этом главнее файла.
    load_env()

    url = dsn or os.environ.get("DATABASE_URL")

    if url:
        from .migrate import migrate
        from .pg import PostgresDatabase
        from .repositories.customers_repo import PgCustomerRepository
        from .repositories.postgres_repo import PgOrderRepository, PgProductRepository
        from .repositories.reviews_repo import PgReviewRepository

        db = PostgresDatabase(url)
        if migrate_schema:
            migrate(db, MIGRATIONS_DIR, verbose=False)
        products = PgProductRepository(db)
        orders = PgOrderRepository(db)
        reviews = PgReviewRepository(db)
        customers = PgCustomerRepository(db)
        kind = "postgres"
    else:
        from .db import Database
        from .repositories.customers_repo import SqliteCustomerRepository
        from .repositories.reviews_repo import SqliteReviewRepository
        from .repositories.sqlite_repo import SqliteOrderRepository, SqliteProductRepository

        # Путь к файлу базы тоже можно передать окружением: uvicorn
        # поднимает приложение отдельным процессом, и аргументы CLI
        # до него не доходят.
        path = sqlite_path or os.environ.get("SHOP_SQLITE_PATH")
        db = Database(path) if path else Database.temporary()
        db.init_schema()
        products = SqliteProductRepository(db)
        orders = SqliteOrderRepository(db)
        reviews = SqliteReviewRepository(db)
        customers = SqliteCustomerRepository(db)
        kind = "sqlite"

    return Backend(
        db=db,
        products=products,
        orders=orders,
        reviews=reviews,
        customers=customers,
        order_service=OrderService(products=products, orders=orders, db=db),
        catalog_service=CatalogService(products),
        kind=kind,
    )
