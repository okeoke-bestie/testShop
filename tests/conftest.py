"""Общие фикстуры.

База поднимается в памяти на каждый тест заново. Это даёт изоляцию:
тесты не зависят от порядка запуска и от мусора, оставленного соседом.
Цена — ноль, потому что sqlite в памяти создаётся за микросекунды.
"""

import pytest

from shopapi.db import Database
from shopapi.repositories.sqlite_repo import SqliteOrderRepository, SqliteProductRepository
from shopapi.services.catalog import CatalogService
from shopapi.services.orders import OrderService


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Счётчик частоты обнуляется перед каждым тестом.

    Ограничитель — единственный на процесс, и без сброса тесты начали бы
    зависеть от порядка запуска: пятая регистрация в прогоне упиралась бы
    в лимит, поставленный совсем другим тестом. Такую связь между тестами
    ищут потом часами.
    """
    try:
        from shopapi import api
    except ImportError:
        # Ядро проекта живёт без FastAPI, и его тесты должны идти
        # без установленных веб-зависимостей. Сбрасывать тогда нечего.
        yield
        return

    api.limiter.reset()
    yield
    api.limiter.reset()


@pytest.fixture
def db():
    """База во временном файле, а не в памяти.

    У каждого соединения к ":memory:" своя отдельная база, поэтому
    потокам пришлось бы делить одно соединение — а это порча состояния
    драйвера (на Python 3.14 сразу InterfaceError). Временный файл даёт
    то, что нужно: одна база, по соединению на поток, как в настоящем
    сервисе. Файл удаляется после теста.
    """
    database = Database.temporary()
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def products(db):
    return SqliteProductRepository(db)


@pytest.fixture
def orders_repo(db):
    return SqliteOrderRepository(db)


@pytest.fixture
def service(db, products, orders_repo):
    return OrderService(products=products, orders=orders_repo, db=db)


@pytest.fixture
def catalog(products):
    return CatalogService(products)


@pytest.fixture
def sample_products(products):
    """Три товара с разными остатками.

    Отдельно есть позиция с остатком 1 — на ней проверяется гонка
    за последним экземпляром.
    """
    return {
        "console": products.add("SKU-001", "Консоль игровая", 4999000, 10),
        "game": products.add("SKU-002", "Игра приключенческая", 349900, 3),
        "rare": products.add("SKU-003", "Коллекционное издание", 1299900, 1),
    }


@pytest.fixture
def reviews(db):
    from shopapi.repositories.reviews_repo import SqliteReviewRepository

    return SqliteReviewRepository(db)


@pytest.fixture
def customers_repo(db):
    from shopapi.repositories.customers_repo import SqliteCustomerRepository

    return SqliteCustomerRepository(db)
