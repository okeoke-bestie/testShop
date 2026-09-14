"""Тесты приёмов работы с базой: N+1, пагинация, транзакции.

Особенность: проблемы производительности проверяются СЧЁТЧИКОМ ЗАПРОСОВ,
а не замером времени. Время нестабильно и зависит от машины, число
запросов — детерминировано. Такой тест сломается ровно тогда, когда
кто-то добавит обращение к базе внутрь цикла.
"""

import pytest

from shopapi.errors import ValidationError
from shopapi.services.orders import OrderRequest

# ---------- N+1 ----------


def test_naive_loading_makes_n_plus_1_queries(db, service, products, orders_repo):
    """Наивная версия: 1 запрос за заказами + по одному на каждый.

    Тест закрепляет ПЛОХОЕ поведение намеренно — чтобы разница была
    измеримой, а не декларативной.
    """
    product = products.add("SKU-N1", "Товар", 100000, 1000)
    for _ in range(10):
        service.create_order(OrderRequest(customer_id=5, items=[(product.id, 1)]))

    with db.counter.measure():
        orders = orders_repo.list_for_customer_nplus1(customer_id=5)

    assert len(orders) == 10
    assert db.counter.count == 11, f"ожидалось 1+10, получено {db.counter.count}"


def test_batched_loading_makes_two_queries(db, service, products, orders_repo):
    """Правильная версия: ровно 2 запроса независимо от числа заказов.

    Это и есть лечение N+1 — строки всех заказов забираются одним
    запросом с IN и раскладываются по заказам в памяти.
    """
    product = products.add("SKU-BATCH", "Товар", 100000, 1000)
    for _ in range(10):
        service.create_order(OrderRequest(customer_id=6, items=[(product.id, 1)]))

    with db.counter.measure():
        orders = orders_repo.list_for_customer(customer_id=6)

    assert len(orders) == 10
    assert db.counter.count == 2, f"ожидалось 2 запроса, получено {db.counter.count}"


def test_batched_loading_does_not_grow_with_data(db, service, products, orders_repo):
    """Главное свойство: число запросов НЕ зависит от объёма данных.

    Именно это отличает исправленный N+1 от «стало быстрее на тестовых
    десяти записях».
    """
    product = products.add("SKU-SCALE", "Товар", 100000, 1000)
    for _ in range(30):
        service.create_order(OrderRequest(customer_id=7, items=[(product.id, 1)]))

    with db.counter.measure():
        orders_repo.list_for_customer(customer_id=7)
    queries_for_30 = db.counter.count

    assert queries_for_30 == 2


def test_get_many_is_single_query(db, products):
    ids = [products.add(f"SKU-M{i}", f"Товар {i}", 1000, 5).id for i in range(20)]

    with db.counter.measure():
        found = products.get_many(ids)

    assert len(found) == 20
    assert db.counter.count == 1


# ---------- пагинация ----------


@pytest.fixture
def many_products(products):
    return [
        products.add(f"SKU-P{i:03d}", f"Товар {i:03d}", 1000 + i, 10)
        for i in range(25)
    ]


def test_pagination_walks_all_items_without_duplicates(products, many_products):
    seen = []
    cursor = None
    pages = 0

    while True:
        page = products.list_page(limit=10, cursor=cursor)
        seen.extend(p.id for p in page.items)
        pages += 1
        if not page.has_more:
            break
        cursor = page.next_cursor
        assert pages < 10, "пагинация зациклилась"

    assert len(seen) == 25
    assert len(set(seen)) == 25, "страницы пересекаются"


def test_last_page_has_no_cursor(products, many_products):
    """Признак конца — отсутствие курсора, а не «пришло меньше, чем просил».

    Второе неверно: часть записей может быть отфильтрована базой,
    и короткая страница не означает конец данных.
    """
    cursor = None
    for _ in range(3):
        page = products.list_page(limit=10, cursor=cursor)
        cursor = page.next_cursor

    assert cursor is None


def test_insert_between_pages_does_not_shift_results(products, many_products):
    """Вставка новой записи не должна сдвигать выдачу.

    С OFFSET это происходит: добавили строку в начало — и одна запись
    покажется на второй странице повторно, а другая пропадёт. Keyset
    берёт строки строго после известной позиции, поэтому не сдвигается.
    """
    first = products.list_page(limit=10)
    first_ids = {p.id for p in first.items}

    products.add("SKU-AAA", "Товар 000-новый", 500, 5)  # встанет в начало

    second = products.list_page(limit=10, cursor=first.next_cursor)
    second_ids = {p.id for p in second.items}

    assert not (first_ids & second_ids), "запись показалась дважды"


def test_invalid_cursor_reports_clear_error(products):
    with pytest.raises(ValidationError):
        products.list_page(cursor="не-курсор-а-мусор")


def test_limit_is_bounded(products):
    """Ограничение сверху — защита от запроса, который выгрузит всю базу."""
    with pytest.raises(ValidationError):
        products.list_page(limit=100_000)
    with pytest.raises(ValidationError):
        products.list_page(limit=0)


# ---------- транзакции ----------


def test_transaction_rolls_back_on_error(db, products):
    product = products.add("SKU-TX", "Товар", 100000, 10)

    with pytest.raises(RuntimeError), db.transaction():
        products.reserve_stock(product.id, 5)
        raise RuntimeError("что-то пошло не так")

    assert products.get(product.id).stock == 10, "изменение не откатилось"


def test_transaction_commits_on_success(db, products):
    product = products.add("SKU-TX2", "Товар", 100000, 10)

    with db.transaction():
        products.reserve_stock(product.id, 3)

    assert products.get(product.id).stock == 7


def test_stock_constraint_blocks_negative(db, products):
    """Ограничение CHECK в схеме — последний рубеж.

    Даже если логика ошибётся, база не даст записать отрицательный
    остаток. Дублирование проверки здесь оправдано: цена ошибки высокая,
    а стоимость ограничения нулевая.
    """
    import sqlite3

    product = products.add("SKU-CHK", "Товар", 100000, 1)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE products SET stock = -5 WHERE id = ?", (product.id,))
