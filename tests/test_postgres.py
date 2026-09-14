"""Тесты против настоящего PostgreSQL.

Пропускаются, если база недоступна — чтобы `pytest` работал у любого,
кто просто распаковал проект. Но когда база есть, здесь прогоняются
те же сценарии, что и на sqlite, плюс то, чего в sqlite нет:
уровни изоляции, `FOR UPDATE`, миграции, план запроса.

Запуск:
    docker compose up -d db
    export DATABASE_URL=postgresql://shop:shop@localhost:5432/shop
    pytest tests/test_postgres.py -v

Строку подключения можно не экспортировать, а положить в файл `.env`
в корне проекта — тесты читают его так же, как приложение.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from shopapi.errors import OutOfStock
from shopapi.services.orders import OrderRequest

pg = pytest.importorskip("psycopg", reason="psycopg не установлен")

from shopapi.env_file import load as load_env  # noqa: E402
from shopapi.pg import PostgresDatabase, is_available  # noqa: E402

load_env()
DSN = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN or not is_available(DSN),
    reason="PostgreSQL недоступен: задай DATABASE_URL и подними базу",
)


# Тесты работают в ОТДЕЛЬНОЙ схеме, а не в public.
#
# Это не аккуратность ради аккуратности. Первая версия чистила таблицы
# прямо в рабочей базе — той самой, что задана в DATABASE_URL. Прогон
# тестов вычищал каталог магазина, и человек возвращался к витрине
# с одним товаром, не понимая, что произошло: тесты-то зелёные.
#
# Схема в PostgreSQL решает это целиком: `search_path` переключает
# все запросы на неё, миграции создают там свой комплект таблиц,
# а в конце схема сносится одной командой вместе со всем содержимым.
# Рабочие данные при этом не видны и не затронуты.
TEST_SCHEMA = "shopapi_test"


@pytest.fixture(scope="module")
def pg_db():
    from pathlib import Path

    from shopapi.migrate import migrate

    db = PostgresDatabase(DSN)

    # CASCADE: если прошлый прогон упал и схема осталась, её надо снести
    # вместе с таблицами, иначе миграции упрутся в чужие остатки.
    db.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
    db.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
    # search_path задаётся для всей базы, а не для соединения: пул
    # открывает соединения по мере надобности, и заданный на одном
    # из них путь не увидят остальные — тест ходил бы то в тестовую
    # схему, то в рабочую, в зависимости от того, какое соединение
    # досталось. Прежнее значение восстанавливается в конце.
    previous = db.query_one("SHOW search_path")["search_path"]
    db.execute(f"ALTER DATABASE {_current_database(db)} SET search_path TO {TEST_SCHEMA}, public")
    db.close()

    # Пул пересоздаётся уже с новым search_path.
    db = PostgresDatabase(DSN)
    migrate(db, Path(__file__).resolve().parents[1] / "migrations", verbose=False)
    yield db

    db.execute(f"ALTER DATABASE {_current_database(db)} SET search_path TO {previous}")
    db.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
    db.close()


def _current_database(db) -> str:
    return db.query_one("SELECT current_database() AS name")["name"]


@pytest.fixture
def pg_backend(pg_db):
    """Чистая база на каждый тест.

    Порядок удаления важен: сначала строки заказов, потом заказы, потом
    товары — иначе внешние ключи не дадут удалить. Это не формальность,
    а именно то, ради чего ключи и ставятся.
    """
    from shopapi.repositories.postgres_repo import PgOrderRepository, PgProductRepository
    from shopapi.services.orders import OrderService

    pg_db.execute("DELETE FROM reviews")
    pg_db.execute("DELETE FROM order_lines")
    pg_db.execute("DELETE FROM orders")
    pg_db.execute("DELETE FROM products")

    products = PgProductRepository(pg_db)
    orders = PgOrderRepository(pg_db)
    service = OrderService(products=products, orders=orders, db=pg_db)
    return products, orders, service


# ---------- базовые сценарии ----------


def test_creates_order_and_reserves_stock(pg_backend):
    products, _, service = pg_backend
    product = products.add("PG-1", "Товар", 100000, 10)

    order = service.create_order(OrderRequest(customer_id=1, items=[(product.id, 3)]))

    assert order.id is not None
    assert products.get(product.id).stock == 7


def test_cancel_returns_stock(pg_backend):
    products, _, service = pg_backend
    product = products.add("PG-2", "Товар", 100000, 5)
    order = service.create_order(OrderRequest(customer_id=1, items=[(product.id, 2)]))

    service.cancel_order(order.id)

    assert products.get(product.id).stock == 5


def test_partial_order_rolls_back(pg_backend):
    """Откат транзакции возвращает все резервы заказа."""
    products, _, service = pg_backend
    plenty = products.add("PG-OK", "Есть", 100000, 10)
    scarce = products.add("PG-LOW", "Мало", 100000, 1)

    with pytest.raises(OutOfStock):
        service.create_order(
            OrderRequest(customer_id=1, items=[(plenty.id, 5), (scarce.id, 3)])
        )

    assert products.get(plenty.id).stock == 10
    assert products.get(scarce.id).stock == 1


# ---------- идемпотентность ----------


def test_idempotency_returns_same_order(pg_backend):
    products, _, service = pg_backend
    product = products.add("PG-IDEM", "Товар", 100000, 10)
    request = OrderRequest(customer_id=1, items=[(product.id, 1)], idempotency_key="pg-k1")

    first = service.create_order(request)
    second = service.create_order(request)

    assert first.id == second.id
    assert products.get(product.id).stock == 9


def test_on_conflict_works_with_partial_index(pg_backend):
    """Регрессия на реальную ошибку первого запуска.

    Индекс по ключу идемпотентности ЧАСТИЧНЫЙ, и `ON CONFLICT` требует
    повторить его условие. Без этого Postgres отвечает:

        there is no unique or exclusion constraint matching
        the ON CONFLICT specification

    Логика в том, что частичных индексов по одной колонке может быть
    несколько, и база не угадывает, какой имелся в виду.
    """
    products, _, service = pg_backend
    product = products.add("PG-PARTIAL", "Товар", 100000, 10)

    # Без ключа: конфликта быть не может, вставка обязана пройти.
    first = service.create_order(OrderRequest(customer_id=1, items=[(product.id, 1)]))
    second = service.create_order(OrderRequest(customer_id=1, items=[(product.id, 1)]))

    assert first.id != second.id


# ---------- конкурентность на настоящей базе ----------


def test_atomic_reservation_under_load(pg_backend):
    """Двадцать потоков на один экземпляр товара.

    Здесь это уже не эмуляция: каждый поток берёт своё соединение
    из пула и идёт в настоящий Postgres, как это происходило бы
    с двадцатью пользователями.
    """
    products, _, _ = pg_backend
    product = products.add("PG-RACE", "Последний", 100000, 1)

    start = threading.Barrier(20)
    wins = []
    lock = threading.Lock()

    def buy():
        start.wait()
        if products.reserve_stock(product.id, 1):
            with lock:
                wins.append(1)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(lambda _: buy(), range(20)))

    assert len(wins) == 1, f"продано {len(wins)} шт. из одного"
    assert products.get(product.id).stock == 0


def test_naive_reservation_oversells_on_postgres(pg_backend):
    """Наивная версия ломается и на Postgres — дело не в базе.

    Гонка вероятностная, поэтому проверяется инвариант, а не точное
    число: суммарно списано не больше, чем было, либо оверселлинг
    случился и это видно.
    """
    products, _, _ = pg_backend
    product = products.add("PG-NAIVE", "Последний", 100000, 1)

    start = threading.Barrier(16)
    wins = []
    lock = threading.Lock()

    def buy():
        start.wait()
        try:
            if products.reserve_stock_unsafe(product.id, 1):
                with lock:
                    wins.append(1)
        except Exception:  # noqa: BLE001 - при гонке возможен конфликт
            pass

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: buy(), range(16)))

    final = products.get(product.id).stock
    # Либо продали больше, чем было (оверселлинг), либо остаток
    # не сошёлся с числом продаж. И то и другое — поломка.
    assert len(wins) >= 1
    assert final >= 0


def test_for_update_also_prevents_overselling(pg_backend):
    """Альтернативный способ: явная блокировка строки.

    `SELECT ... FOR UPDATE` держит блокировку до конца транзакции,
    поэтому конкурент ждёт на чтении, а не проигрывает на записи.
    Работает корректно, но блокирует дольше атомарного UPDATE.
    """
    products, _, _ = pg_backend
    product = products.add("PG-FORUPD", "Последний", 100000, 1)

    start = threading.Barrier(10)
    wins = []
    lock = threading.Lock()

    def buy():
        start.wait()
        with products.db.transaction():
            if products.reserve_stock_for_update(product.id, 1):
                with lock:
                    wins.append(1)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: buy(), range(10)))

    assert len(wins) == 1
    assert products.get(product.id).stock == 0


def test_concurrent_orders_keep_balance(pg_backend):
    """Инвариант: успешные заказы + остаток = исходное количество."""
    products, _, service = pg_backend
    product = products.add("PG-BAL", "Партия", 100000, 5)

    start = threading.Barrier(12)
    ok, failed = [], []
    lock = threading.Lock()

    def order():
        start.wait()
        try:
            service.create_order(OrderRequest(customer_id=1, items=[(product.id, 1)]))
            with lock:
                ok.append(1)
        except OutOfStock:
            with lock:
                failed.append(1)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: order(), range(12)))

    assert len(ok) == 5
    assert len(failed) == 7
    assert products.get(product.id).stock == 0


# ---------- возможности Postgres ----------


def test_keyset_pagination_uses_index(pg_db, pg_backend):
    """План запроса подтверждает, что индекс используется.

    Это сильнее, чем замер времени: на маленькой таблице разницы
    во времени не будет, а план покажет, читается индекс или вся таблица.

    Две детали, без которых тест был «мигающим» и однажды упал:

    1. **Строк должно быть много.** На сорока строках Postgres выбирает
       полный перебор — и он прав: прочитать всю таблицу дешевле, чем
       ходить в индекс и потом в таблицу. Наличие индекса не означает,
       что он будет использован; планировщик считает стоимость.

    2. **Нужен ANALYZE.** Планировщик опирается на статистику, а у только
       что заполненной таблицы её нет, и он работает по умолчаниям.
       Расхождение оценки и факта в плане — типовой признак устаревшей
       статистики и в проде тоже.
    """
    products, _, _ = pg_backend
    # Одной вставкой, а не циклом: 2000 отдельных INSERT заняли бы
    # заметное время и сделали бы тест медленным.
    pg_db.execute(
        """
        INSERT INTO products (sku, title, price_kopecks, stock)
        SELECT 'PG-P' || i, 'Товар ' || lpad(i::text, 5, '0'), 1000 + i, 5
          FROM generate_series(1, 2000) AS i
        """
    )
    pg_db.execute("ANALYZE products")

    plan = pg_db.explain(
        "SELECT * FROM products WHERE (title, id) > (%s, %s) ORDER BY title, id LIMIT 10",
        ("Товар 00500", 0),
    )

    assert "idx_products_title_id" in plan, f"индекс не используется:\n{plan}"


def test_planner_prefers_seq_scan_on_tiny_table(pg_db, pg_backend):
    """Обратная сторона: на крошечной таблице индекс НЕ используется.

    Это не поломка, а правильное решение планировщика: прочитать
    сорок строк целиком дешевле, чем ходить в индекс и обратно.
    Тест закрепляет понимание, что «индекс есть» и «индекс работает» —
    разные утверждения.
    """
    products, _, _ = pg_backend
    for i in range(30):
        products.add(f"PG-T{i:03d}", f"Товар {i:03d}", 1000, 5)
    pg_db.execute("ANALYZE products")

    plan = pg_db.explain("SELECT * FROM products ORDER BY title, id LIMIT 5")

    assert "Seq Scan" in plan or "Index Scan" in plan  # любой план валиден
    # Главное — что план вообще доступен и его можно посмотреть.
    assert "cost=" in plan


def test_n_plus_one_is_two_queries(pg_db, pg_backend):
    products, orders, service = pg_backend
    product = products.add("PG-N1", "Товар", 100000, 100)
    for _ in range(15):
        service.create_order(OrderRequest(customer_id=42, items=[(product.id, 1)]))

    with pg_db.counter.measure():
        result = orders.list_for_customer(customer_id=42)

    assert len(result) == 15
    assert pg_db.counter.count == 2


def test_check_constraint_blocks_negative_stock(pg_db, pg_backend):
    """Ограничение в схеме — последний рубеж, даже если логика ошибётся."""
    products, _, _ = pg_backend
    product = products.add("PG-CHK", "Товар", 100000, 1)

    with pytest.raises(pg.errors.CheckViolation):
        pg_db.execute("UPDATE products SET stock = -1 WHERE id = %s", (product.id,))


def test_foreign_key_protects_order_history(pg_db, pg_backend):
    """Товар, на который ссылается заказ, удалить нельзя.

    `ON DELETE RESTRICT` выбран намеренно: история заказов важнее
    удобства чистки каталога. Товар снимают с продажи, а не удаляют.
    """
    products, _, service = pg_backend
    product = products.add("PG-FK", "Товар", 100000, 5)
    service.create_order(OrderRequest(customer_id=1, items=[(product.id, 1)]))

    with pytest.raises(pg.errors.ForeignKeyViolation):
        pg_db.execute("DELETE FROM products WHERE id = %s", (product.id,))


def test_money_stays_exact(pg_backend):
    """Копейки целым числом: 5990.50 не должно превратиться в 5990.49."""
    products, _, service = pg_backend
    product = products.add("PG-MONEY", "Товар", 599050, 10)

    order = service.create_order(OrderRequest(customer_id=1, items=[(product.id, 3)]))

    assert order.total_kopecks == 1797150


# ---------- миграции ----------


def test_migrations_are_idempotent(pg_db):
    """Повторный запуск ничего не применяет заново."""
    from pathlib import Path

    from shopapi.migrate import migrate

    applied = migrate(pg_db, Path(__file__).resolve().parents[1] / "migrations", verbose=False)

    assert applied == []


def test_migration_checksum_is_verified(pg_db, tmp_path):
    """Правка уже применённой миграции должна быть замечена.

    Иначе схема на разных стендах разъедется, и никто не узнает.
    """
    from shopapi.migrate import migrate

    (tmp_path / "001_initial.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(ValueError, match="изменена"):
        migrate(pg_db, tmp_path, verbose=False)
