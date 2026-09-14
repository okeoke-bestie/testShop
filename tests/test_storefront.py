"""Витрина должна работать на ОБЕИХ базах.

Эти тесты появились из настоящего бага. Ручки витрины (`list_all`,
`categories`, `get_full`, `recent`) сначала были написаны только
в постгресовом репозитории. Приложение поднималось на sqlite без единой
ошибки — и падало с `AttributeError` в момент первого запроса каталога.

Вывод, который эти тесты закрепляют: «оба репозитория реализуют один
интерфейс» — это утверждение, которое надо проверять, а не держать
в голове. Питон не проверит его за нас: методов просто нет, пока
кто-нибудь их не вызовет.
"""

from __future__ import annotations

import inspect

import pytest

from shopapi.repositories.sqlite_repo import SqliteOrderRepository, SqliteProductRepository
from shopapi.services.orders import OrderRequest

# Методы, без которых витрина не работает.
PRODUCT_API = ["add", "get", "get_full", "get_many", "list_all", "list_page",
               "categories", "reserve_stock", "release_stock", "set_stock",
               "delete_all"]
ORDER_API = ["create", "get", "get_full", "recent", "find_by_idempotency_key",
             "list_for_customer", "set_status"]


def _pg_repos():
    """Постгресовые классы импортируются без подключения к базе.

    Сравнивать интерфейсы можно и без сервера: нас интересуют сигнатуры,
    а не данные. Поэтому этот тест не пропускается на машине без Postgres —
    а расхождение интерфейсов ловится именно там, где его обычно и вносят.
    """
    pytest.importorskip("psycopg", reason="psycopg не установлен")
    from shopapi.repositories.postgres_repo import PgOrderRepository, PgProductRepository

    return PgProductRepository, PgOrderRepository


@pytest.mark.parametrize("name", PRODUCT_API)
def test_sqlite_product_repo_has_storefront_method(name):
    assert hasattr(SqliteProductRepository, name), (
        f"витрина вызовет {name}() и упадёт с AttributeError"
    )


@pytest.mark.parametrize("name", ORDER_API)
def test_sqlite_order_repo_has_storefront_method(name):
    assert hasattr(SqliteOrderRepository, name)


def test_repositories_agree_on_signatures():
    """Одинаковые имена мало: важны и аргументы.

    Если у одной реализации `add` без описания и категории, вызов
    из загрузчика каталога упадёт на TypeError — ровно то, что раньше
    маскировалось запасным вызовом.
    """
    pg_products, pg_orders = _pg_repos()
    pairs = [(SqliteProductRepository, pg_products, PRODUCT_API),
             (SqliteOrderRepository, pg_orders, ORDER_API)]

    for sqlite_cls, pg_cls, names in pairs:
        for name in names:
            if not hasattr(pg_cls, name):
                continue
            ours = inspect.signature(getattr(sqlite_cls, name))
            theirs = inspect.signature(getattr(pg_cls, name))
            assert list(ours.parameters) == list(theirs.parameters), (
                f"{name}(): у sqlite {list(ours.parameters)}, "
                f"у postgres {list(theirs.parameters)}"
            )


# ---------- поведение, а не только наличие ----------

def test_list_all_returns_storefront_fields(products):
    products.add("SKU-1", "Консоль Nova", 4999000, 3,
                 "Мощная консоль", "Консоли", "🎮")
    rows = products.list_all()
    assert rows[0]["description"] == "Мощная консоль"
    assert rows[0]["category"] == "Консоли"
    assert rows[0]["emoji"] == "🎮"


def test_list_all_filters_by_category(products):
    products.add("SKU-1", "Консоль", 100, 1, "", "Консоли", "🎮")
    products.add("SKU-2", "Игра", 100, 1, "", "Игры", "💿")
    assert [r["sku"] for r in products.list_all(category="Игры")] == ["SKU-2"]
    assert len(products.list_all(category="all")) == 2


def test_search_is_case_insensitive_for_cyrillic(products):
    """Ловушка sqlite: встроенный LIKE регистронезависим только к латинице.

    Каталог русский, поэтому обе стороны приводятся к нижнему регистру
    явно. Без этого поиск «консоль» не находил бы «Консоль».
    """
    products.add("SKU-1", "Консоль Nova", 100, 1, "", "Консоли", "🎮")
    assert len(products.list_all(search="консоль")) == 1
    assert len(products.list_all(search="КОНСОЛЬ")) == 1


def test_search_looks_in_description(products):
    products.add("SKU-1", "Геймпад", 100, 1, "беспроводной, с отдачей", "Гаджеты", "🎯")
    assert len(products.list_all(search="беспроводной")) == 1


def test_categories_counts_products(products):
    products.add("SKU-1", "А", 100, 2, "", "Игры", "💿")
    products.add("SKU-2", "Б", 100, 3, "", "Игры", "💿")
    products.add("SKU-3", "В", 100, 1, "", "Консоли", "🎮")
    by_name = {c["category"]: c for c in products.categories()}
    assert by_name["Игры"]["count"] == 2
    assert by_name["Игры"]["total_stock"] == 5


def test_recent_orders_include_totals(products, orders_repo, service):
    product = products.add("SKU-1", "Игра", 250000, 10, "", "Игры", "💿")
    service.create_order(OrderRequest(customer_id=1, items=[(product.id, 2)]))

    recent = orders_repo.recent(limit=5)

    assert len(recent) == 1
    assert recent[0]["items"] == 2
    assert recent[0]["total_kopecks"] == 500000


def test_recent_is_empty_without_orders(orders_repo):
    assert orders_repo.recent() == []


def test_set_stock_changes_value_and_bumps_version(products):
    product = products.add("SKU-1", "Игра", 100, 5)
    row = products.set_stock("SKU-1", 42)
    assert row["stock"] == 42
    assert row["version"] == product.version + 1


def test_set_stock_returns_none_for_unknown_sku(products):
    assert products.set_stock("НЕТ-ТАКОГО", 1) is None


# ---------- обновление старого файла базы ----------

def test_old_database_file_gets_new_columns(tmp_path):
    """Файл, созданный прошлой версией, должен продолжить работать.

    `CREATE TABLE IF NOT EXISTS` существующую таблицу не меняет, поэтому
    без досоздания колонок витрина падала бы на `no such column` —
    и не на старте, а при первом запросе каталога, то есть у пользователя.
    """
    import sqlite3

    from shopapi.db import Database

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE products (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sku           TEXT    NOT NULL UNIQUE,
            title         TEXT    NOT NULL,
            price_kopecks INTEGER NOT NULL,
            stock         INTEGER NOT NULL,
            version       INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO products (sku, title, price_kopecks, stock)
        VALUES ('OLD-1', 'Товар из старой базы', 100000, 7);
    """)
    old.commit()
    old.close()

    db = Database(str(path))
    db.init_schema()
    products = SqliteProductRepository(db)

    rows = products.list_all()

    assert len(rows) == 1, "старые товары должны остаться на месте"
    assert rows[0]["stock"] == 7
    assert rows[0]["category"] == "Прочее", "колонка добавлена со значением по умолчанию"
    assert rows[0]["emoji"] == "📦"
    db.close()


def test_upgrade_runs_twice_without_error(tmp_path):
    """Идемпотентность: повторный запуск не должен падать на 'duplicate column'."""
    from shopapi.db import Database

    path = tmp_path / "twice.sqlite3"
    for _ in range(2):
        db = Database(str(path))
        db.init_schema()
        db.close()


# ---------- фильтр по платформе ----------

def test_list_all_filters_by_platform(products):
    products.add("SKU-1", "Игра для PS", 100, 1, "", "Игры", "💿", "PlayStation 5")
    products.add("SKU-2", "Игра для Switch", 100, 1, "", "Игры", "💿", "Nintendo Switch")

    rows = products.list_all(platform="Nintendo Switch")

    assert [r["sku"] for r in rows] == ["SKU-2"]
    assert len(products.list_all(platform="all")) == 2


def test_platform_and_category_filters_combine(products):
    products.add("SKU-1", "Консоль", 100, 1, "", "Консоли", "🎮", "PlayStation 5")
    products.add("SKU-2", "Игра", 100, 1, "", "Игры", "💿", "PlayStation 5")
    products.add("SKU-3", "Игра", 100, 1, "", "Игры", "💿", "Nintendo Switch")

    rows = products.list_all(category="Игры", platform="PlayStation 5")

    assert [r["sku"] for r in rows] == ["SKU-2"], "фильтры должны складываться через AND"


def test_platforms_counts_and_skips_empty(products):
    products.add("SKU-1", "А", 100, 2, "", "Игры", "💿", "PlayStation 5")
    products.add("SKU-2", "Б", 100, 3, "", "Игры", "💿", "PlayStation 5")
    products.add("SKU-3", "В", 100, 1, "", "Игры", "💿", "Nintendo Switch")
    products.add("SKU-4", "Без платформы", 100, 1)

    rows = products.platforms()

    by_name = {p["platform"]: p for p in rows}
    assert by_name["PlayStation 5"]["count"] == 2
    assert by_name["PlayStation 5"]["total_stock"] == 5
    assert "" not in by_name, "товар без платформы не должен давать пустую вкладку"


def test_search_looks_in_developer(products):
    """Поиск по студии: «Naughty Dog» — это запрос, который человек вводит."""
    products.add("SKU-1", "Игра", 100, 1, "описание", "Игры", "💿",
                 "PlayStation 5", "Боевик", "Naughty Dog", 2020)
    assert len(products.list_all(search="naughty")) == 1


def test_reference_fields_survive_round_trip(products):
    products.add("SKU-1", "Игра", 100, 1, "описание", "Игры", "💿",
                 "PlayStation 5", "Боевик", "Santa Monica Studio", 2018)

    row = products.list_all()[0]

    assert row["platform"] == "PlayStation 5"
    assert row["genre"] == "Боевик"
    assert row["developer"] == "Santa Monica Studio"
    assert row["year"] == 2018
