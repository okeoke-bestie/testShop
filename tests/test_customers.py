"""Профили покупателей и право оставить отзыв.

Главная мысль этих тестов: «отзыв только после покупки» — правило
сервера, а не интерфейса. Кнопку на витрине можно спрятать, но POST
на ручку отправит кто угодно из консоли браузера, и проверка должна
жить там, где её нельзя обойти.
"""

from __future__ import annotations

import inspect

import pytest

from shopapi.errors import NotFound, ValidationError
from shopapi.repositories.customers_repo import SqliteCustomerRepository
from shopapi.services.orders import OrderRequest


@pytest.fixture
def customers(db):
    return SqliteCustomerRepository(db)


# ---------- профиль ----------

def test_saves_and_reads_back(customers):
    customer_id = customers.add(name="  Аня  ", email="  ANYA@example.com ", city="Казань")
    row = customers.get(customer_id)
    assert row["name"] == "Аня"
    assert row["email"] == "anya@example.com", "почта приводится к нижнему регистру"
    assert row["city"] == "Казань"


def test_rejects_bad_email(customers):
    with pytest.raises(ValidationError, match="почта"):
        customers.add(name="Аня", email="не-почта")


def test_rejects_empty_name(customers):
    with pytest.raises(ValidationError, match="имя"):
        customers.add(name="   ", email="a@example.com")


def test_email_is_unique_in_database(customers):
    """Уникальность обеспечивает БАЗА, а не проверка в коде.

    Проверка «сначала SELECT, потом INSERT» — гонка: два параллельных
    запроса оба увидят, что почты нет, и оба вставят.
    """
    import sqlite3

    customers.add(name="Аня", email="a@example.com")
    with pytest.raises(sqlite3.IntegrityError):
        customers.add(name="Другая Аня", email="a@example.com")


def test_unknown_customer_raises(customers):
    with pytest.raises(NotFound):
        customers.get(99999)


def test_find_by_email_is_case_insensitive(customers):
    customers.add(name="Аня", email="anya@example.com")
    assert customers.find_by_email("ANYA@EXAMPLE.COM")["name"] == "Аня"
    assert customers.find_by_email("нет@example.com") is None


# ---------- право на отзыв ----------

def test_has_not_purchased_by_default(customers, products):
    customer_id = customers.add(name="Аня", email="a@example.com")
    product = products.add("SKU-1", "Игра", 100000, 5)
    assert customers.has_purchased(customer_id, product.id) is False


def test_purchase_grants_right_to_review(customers, products, service):
    customer_id = customers.add(name="Аня", email="a@example.com")
    product = products.add("SKU-1", "Игра", 100000, 5)

    service.create_order(OrderRequest(customer_id=customer_id, items=[(product.id, 1)]))

    assert customers.has_purchased(customer_id, product.id) is True


def test_cancelled_order_takes_the_right_away(customers, products, service):
    """Отменённый заказ — не покупка: товар вернулся на склад.

    Иначе отзыв можно было бы получить бесплатно: заказать, написать,
    отменить.
    """
    customer_id = customers.add(name="Аня", email="a@example.com")
    product = products.add("SKU-1", "Игра", 100000, 5)
    order = service.create_order(
        OrderRequest(customer_id=customer_id, items=[(product.id, 1)])
    )

    service.cancel_order(order.id)

    assert customers.has_purchased(customer_id, product.id) is False


def test_purchase_of_one_product_does_not_unlock_another(customers, products, service):
    customer_id = customers.add(name="Аня", email="a@example.com")
    bought = products.add("SKU-1", "Куплено", 100000, 5)
    other = products.add("SKU-2", "Не куплено", 100000, 5)

    service.create_order(OrderRequest(customer_id=customer_id, items=[(bought.id, 1)]))

    assert customers.has_purchased(customer_id, bought.id) is True
    assert customers.has_purchased(customer_id, other.id) is False


def test_purchase_of_one_customer_does_not_unlock_another(customers, products, service):
    """Покупка одного человека не даёт права другому — очевидно,
    но именно такие условия теряются при правке WHERE."""
    first = customers.add(name="Аня", email="a@example.com")
    second = customers.add(name="Боря", email="b@example.com")
    product = products.add("SKU-1", "Игра", 100000, 5)

    service.create_order(OrderRequest(customer_id=first, items=[(product.id, 1)]))

    assert customers.has_purchased(first, product.id) is True
    assert customers.has_purchased(second, product.id) is False


# ---------- покупки ----------

def test_purchased_products_groups_repeats(customers, products, service):
    """Один товар, купленный дважды, — одна строка в списке покупок."""
    customer_id = customers.add(name="Аня", email="a@example.com")
    product = products.add("SKU-1", "Игра", 100000, 10)

    service.create_order(OrderRequest(customer_id=customer_id, items=[(product.id, 1)]))
    service.create_order(OrderRequest(customer_id=customer_id, items=[(product.id, 2)]))

    rows = customers.purchased_products(customer_id)

    assert len(rows) == 1
    assert rows[0]["total_quantity"] == 3


def test_purchased_products_excludes_cancelled(customers, products, service):
    customer_id = customers.add(name="Аня", email="a@example.com")
    product = products.add("SKU-1", "Игра", 100000, 5)
    order = service.create_order(
        OrderRequest(customer_id=customer_id, items=[(product.id, 1)])
    )

    service.cancel_order(order.id)

    assert customers.purchased_products(customer_id) == []


def test_purchases_do_not_cause_n_plus_one(customers, products, service, db):
    """Список покупок — один запрос, сколько бы товаров ни было."""
    customer_id = customers.add(name="Аня", email="a@example.com")
    for i in range(12):
        product = products.add(f"SKU-{i}", f"Игра {i}", 1000, 5)
        service.create_order(OrderRequest(customer_id=customer_id, items=[(product.id, 1)]))

    with db.counter.measure():
        rows = customers.purchased_products(customer_id)

    assert len(rows) == 12
    assert db.counter.count == 1, f"ожидался один запрос, сделано {db.counter.count}"


def test_list_all_counts_orders_and_reviews(customers, products, service, reviews):
    customer_id = customers.add(name="Аня", email="a@example.com")
    product = products.add("SKU-1", "Игра", 100000, 5)
    service.create_order(OrderRequest(customer_id=customer_id, items=[(product.id, 1)]))
    reviews.add(author="Аня", rating=5, body="текст",
                product_id=product.id, customer_id=customer_id)

    row = next(c for c in customers.list_all() if c["id"] == customer_id)

    assert row["orders_count"] == 1
    assert row["reviews_count"] == 1


# ---------- один отзыв на товар ----------

def test_one_review_per_customer_and_product(customers, products, reviews):
    """Ограничение стоит в БАЗЕ уникальным индексом.

    Проверка в коде здесь не годится по той же причине, что и с почтой:
    два параллельных запроса оба увидят, что отзыва нет.
    """
    import sqlite3

    customer_id = customers.add(name="Аня", email="a@example.com")
    product = products.add("SKU-1", "Игра", 100000, 5)

    reviews.add(author="Аня", rating=5, body="первый",
                product_id=product.id, customer_id=customer_id)

    with pytest.raises(sqlite3.IntegrityError):
        reviews.add(author="Аня", rating=4, body="второй",
                    product_id=product.id, customer_id=customer_id)


def test_same_customer_can_review_different_products(customers, products, reviews):
    customer_id = customers.add(name="Аня", email="a@example.com")
    first = products.add("SKU-1", "Игра", 100000, 5)
    second = products.add("SKU-2", "Другая", 100000, 5)

    reviews.add(author="Аня", rating=5, body="раз", product_id=first.id,
                customer_id=customer_id)
    reviews.add(author="Аня", rating=4, body="два", product_id=second.id,
                customer_id=customer_id)

    assert len(reviews.list_recent()) == 2


def test_several_reviews_about_shop_are_allowed(customers, reviews):
    """Уникальность частичная: отзыв без товара под неё не подпадает.

    Иначе человек не смог бы дважды написать о магазине, а это
    нормальная ситуация — заказы разные.
    """
    customer_id = customers.add(name="Аня", email="a@example.com")

    reviews.add(author="Аня", rating=5, body="раз", customer_id=customer_id)
    reviews.add(author="Аня", rating=4, body="два", customer_id=customer_id)

    assert len(reviews.list_recent()) == 2


def test_reviewed_product_ids_is_not_limited_by_page_size(customers, products, reviews):
    """Регрессия: «последние N» нельзя использовать как «все».

    Первая версия брала общий список отзывов с лимитом и отбирала
    из него свои. На большом числе отзывов старые записи покупателя
    в окно не попадали, профиль предлагал оценить уже оценённое,
    а сервер отвечал 409.
    """
    customer_id = customers.add(name="Аня", email="a@example.com")
    mine = products.add("MY-1", "Мой товар", 1000, 5)
    reviews.add(author="Аня", rating=5, body="мой отзыв",
                product_id=mine.id, customer_id=customer_id)

    # Много чужих отзывов сверху — они вытеснили бы мой из любого окна.
    for i in range(250):
        other = products.add(f"OTHER-{i}", f"Товар {i}", 1000, 5)
        reviews.add(author=f"Кто-то {i}", rating=5, body="текст", product_id=other.id)

    assert mine.id in reviews.reviewed_product_ids(customer_id)


# ---------- интерфейсы двух баз ----------

def test_customer_repositories_agree_on_signatures():
    pytest.importorskip("psycopg", reason="psycopg не установлен")
    from shopapi.repositories.customers_repo import PgCustomerRepository

    for name in ["add", "get", "find_by_email", "list_all",
                 "purchased_products", "has_purchased", "delete_demo"]:
        ours = inspect.signature(getattr(SqliteCustomerRepository, name))
        theirs = inspect.signature(getattr(PgCustomerRepository, name))
        assert list(ours.parameters) == list(theirs.parameters), name
