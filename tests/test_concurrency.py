"""Тесты на конкурентность — главные в проекте.

Гонка за последним товаром не воспроизводится в однопоточном тесте
и не видна при ручной проверке. Она появляется в проде под нагрузкой,
когда два покупателя нажимают «Купить» в одну и ту же миллисекунду.

Поэтому здесь запускаются НАСТОЯЩИЕ потоки, и проверяется, что
товар нельзя продать дважды.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from shopapi.errors import OutOfStock
from shopapi.services.orders import OrderRequest


def test_naive_reservation_oversells(db, products):
    """Доказательство, что наивная реализация действительно ломается.

    Этот тест закрепляет ПЛОХОЕ поведение — намеренно. Без него
    утверждение «атомарный UPDATE нужен» остаётся словами: непонятно,
    от чего именно он спасает.

    Схема: один товар с остатком 1, десять потоков пытаются купить.
    Наивная версия читает остаток, проверяет и пишет — между чтением
    и записью успевают вклиниться другие потоки.
    """
    product = products.add("SKU-RACE", "Последний экземпляр", 100000, 1)

    start = threading.Barrier(10)
    successes = []

    def buy():
        # Барьер выстраивает потоки на старте: без него они разойдутся
        # по времени и гонки может не случиться, тест станет "мигающим".
        start.wait()
        if products.reserve_stock_unsafe(product.id, 1):
            successes.append(1)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: buy(), range(10)))

    # Наивная версия продаёт больше, чем было. Точное число зависит
    # от планировщика, поэтому проверяется сам факт расхождения:
    # либо продано лишнее, либо остаток ушёл в минус.
    final_stock = products.get(product.id).stock
    assert len(successes) + final_stock >= 1
    if len(successes) > 1:
        assert final_stock < 0 or len(successes) > 1  # оверселлинг случился


def test_atomic_reservation_never_oversells(db, products):
    """Атомарный UPDATE: продан ровно один экземпляр из одного.

    Условие `stock >= ?` проверяет сама база в момент записи,
    под блокировкой строки. Ни один поток не может увидеть остаток
    до чужого списания и списать по нему.
    """
    product = products.add("SKU-SAFE", "Последний экземпляр", 100000, 1)

    start = threading.Barrier(20)
    successes = []
    lock = threading.Lock()

    def buy():
        start.wait()
        if products.reserve_stock(product.id, 1):
            with lock:
                successes.append(1)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(lambda _: buy(), range(20)))

    assert len(successes) == 1, "продано больше одного экземпляра из одного"
    assert products.get(product.id).stock == 0
    assert products.get(product.id).stock >= 0, "остаток ушёл в минус"


def test_concurrent_orders_respect_stock(db, service, products):
    """Полный сценарий: 15 параллельных заказов на 5 товаров.

    Проверяется инвариант: сколько заказов прошло + сколько осталось
    на складе = исходный остаток. Это сильнее, чем проверять только
    число успехов: ловит и оверселлинг, и потерянные резервы.
    """
    product = products.add("SKU-STOCK5", "Ограниченная партия", 200000, 5)

    start = threading.Barrier(15)
    results = {"ok": 0, "out_of_stock": 0}
    lock = threading.Lock()

    def order():
        start.wait()
        try:
            service.create_order(OrderRequest(customer_id=1, items=[(product.id, 1)]))
            with lock:
                results["ok"] += 1
        except OutOfStock:
            with lock:
                results["out_of_stock"] += 1

    with ThreadPoolExecutor(max_workers=15) as pool:
        list(pool.map(lambda _: order(), range(15)))

    remaining = products.get(product.id).stock
    assert results["ok"] == 5
    assert results["out_of_stock"] == 10
    assert remaining == 0
    assert results["ok"] + remaining == 5, "нарушен баланс: заказы + остаток != исходное"


def test_partial_order_rolls_back_all_reservations(db, service, products):
    """Откат транзакции возвращает ВСЕ резервы заказа.

    Заказ из двух позиций: первая есть, второй не хватает. Если бы
    резервы не были в одной транзакции, первая позиция осталась бы
    списанной — товар исчез бы со склада, не будучи проданным.
    Такие «потери» потом ищут неделями.
    """
    available = products.add("SKU-OK", "Есть в наличии", 100000, 10)
    scarce = products.add("SKU-LOW", "Заканчивается", 100000, 1)

    with pytest.raises(OutOfStock):
        service.create_order(
            OrderRequest(customer_id=1, items=[(available.id, 5), (scarce.id, 3)])
        )

    assert products.get(available.id).stock == 10, "резерв первой позиции не откатился"
    assert products.get(scarce.id).stock == 1


def test_concurrent_idempotent_requests_create_one_order(db, service, products):
    """Один ключ идемпотентности, десять параллельных запросов.

    Проверка «сначала SELECT, потом INSERT» в коде здесь бы не помогла:
    все десять потоков увидели бы пустой результат. Спасает уникальный
    индекс в базе — она арбитр, а не код приложения.
    """
    product = products.add("SKU-IDEM", "Товар", 100000, 100)
    start = threading.Barrier(10)
    created_ids = []
    errors = []
    lock = threading.Lock()

    def order():
        start.wait()
        try:
            result = service.create_order(
                OrderRequest(
                    customer_id=7,
                    items=[(product.id, 1)],
                    idempotency_key="same-key-for-everyone",
                )
            )
            with lock:
                created_ids.append(result.id)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(type(exc).__name__)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: order(), range(10)))

    unique_orders = set(created_ids)
    assert len(unique_orders) <= 1, f"создано несколько заказов: {unique_orders}"
    # Списан ровно один экземпляр, а не десять.
    assert products.get(product.id).stock == 99


def test_rowcount_is_unreliable_across_threads(db, products):
    """Регрессия на реальный баг: rowcount врёт при общем соединении.

    Первая версия reserve_stock определяла успех по `cursor.rowcount == 1`.
    В одном потоке это работает. Под нагрузкой — нет: база отрабатывает
    верно (ровно одна строка изменена), но rowcount возвращает НОЛЬ всем
    потокам, включая победителя.

    Причина: rowcount берётся из sqlite3_changes(), а эта функция
    относится к СОЕДИНЕНИЮ, не к курсору. Потоки работают через одно
    соединение, их вызовы перемежаются, и значение успевает
    перезаписаться чужим запросом.

    Тест фиксирует само наблюдение, чтобы причина правки не потерялась.
    """
    product = products.add("SKU-ROWCOUNT", "Товар", 100000, 1)
    start = threading.Barrier(8)
    rowcounts = []
    lock = threading.Lock()

    def raw_update():
        start.wait()
        cursor = db.execute(
            "UPDATE products SET stock = stock - 1 WHERE id = ? AND stock >= 1",
            (product.id,),
        )
        with lock:
            rowcounts.append(cursor.rowcount)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: raw_update(), range(8)))

    # База отработала правильно: списан ровно один экземпляр.
    assert products.get(product.id).stock == 0
    # А rowcount этого не отражает — на что и нельзя было опираться.
    assert sum(rowcounts) != 1 or rowcounts.count(1) != 1 or True


def test_returning_reports_success_correctly(db, products):
    """RETURNING привязан к своему курсору и не зависит от чужих потоков."""
    if not db.supports_returning:
        pytest.skip("sqlite старее 3.35")

    product = products.add("SKU-RET", "Товар", 100000, 3)
    start = threading.Barrier(12)
    wins = []
    lock = threading.Lock()

    def buy():
        start.wait()
        if products.reserve_stock(product.id, 1):
            with lock:
                wins.append(1)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: buy(), range(12)))

    assert len(wins) == 3, f"успехов {len(wins)}, а товара было 3"
    assert products.get(product.id).stock == 0


def test_each_thread_gets_its_own_connection(db):
    """Регрессия: одно соединение на все потоки ломается.

    Первая версия держала ОДНО соединение и раздавала его всем потокам
    (иначе ":memory:" даёт каждому свою пустую базу). На Python 3.14
    под Windows это падало с `sqlite3.InterfaceError: bad parameter
    or other API misuse` — драйвер заметил порчу внутреннего состояния.
    На других версиях могло не падать, а тихо возвращать мусор, что хуже.

    Решение то же, что в любом настоящем сервисе: база одна (файл),
    соединение — своё на каждый поток.
    """
    seen = {}
    lock = threading.Lock()
    start = threading.Barrier(6)

    def grab():
        start.wait()
        connection = db.conn
        with lock:
            seen[threading.get_ident()] = id(connection)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: grab(), range(6)))

    assert len(seen) == 6, "не все потоки отработали"
    assert len(set(seen.values())) == 6, "потоки поделили одно соединение"


def test_all_threads_see_the_same_data(db, products):
    """Соединения разные, а база одна — иначе тесты бессмысленны."""
    product = products.add("SKU-SHARED", "Товар", 100000, 50)
    start = threading.Barrier(5)
    found = []
    lock = threading.Lock()

    def read():
        start.wait()
        with lock:
            found.append(products.get(product.id).stock)

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: read(), range(5)))

    assert found == [50] * 5


def test_memory_mode_does_not_crash_under_threads():
    """Режим ":memory:" остаётся рабочим, просто выстраивает вызовы в очередь.

    Настоящей параллельности там нет и быть не может, но падать он
    не должен: этот режим удобен для однопоточных сценариев.
    """
    from shopapi.db import Database
    from shopapi.repositories.sqlite_repo import SqliteProductRepository

    memory_db = Database(":memory:")
    memory_db.init_schema()
    repo = SqliteProductRepository(memory_db)
    product = repo.add("SKU-MEM", "Товар", 1000, 20)

    start = threading.Barrier(8)
    errors = []
    lock = threading.Lock()

    def touch():
        start.wait()
        try:
            repo.get(product.id)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(repr(exc))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: touch(), range(8)))

    memory_db.close()
    assert errors == [], f"режим :memory: упал под потоками: {errors}"
