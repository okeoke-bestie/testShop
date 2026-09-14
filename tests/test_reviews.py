"""Отзывы: проверки, связи, сводка.

Отдельная тема здесь — демонстрационные данные. Отзыв, выдуманный
для витрины, обязан оставаться отличимым от настоящего: иначе рано
или поздно кто-нибудь выгрузит таблицу и предъявит её как мнения
покупателей. Флаг `is_demo` — это не украшение интерфейса, а часть
контракта, и потому он проверяется тестом.
"""

from __future__ import annotations

import inspect

import pytest

from shopapi.errors import ValidationError
from shopapi.repositories.reviews_repo import SqliteReviewRepository
from shopapi.reviews_file import ReviewItem, seed_reviews


@pytest.fixture
def reviews(db):
    return SqliteReviewRepository(db)


# ---------- проверки до базы ----------

def test_rejects_empty_author(reviews):
    with pytest.raises(ValidationError, match="имя не заполнено"):
        reviews.add(author="  ", rating=5, body="текст")


def test_rejects_empty_body(reviews):
    with pytest.raises(ValidationError, match="текст отзыва не заполнен"):
        reviews.add(author="Аня", rating=5, body="   ")


@pytest.mark.parametrize("rating", [0, 6, -1, 100])
def test_rejects_rating_outside_range(reviews, rating):
    with pytest.raises(ValidationError, match="от 1 до 5"):
        reviews.add(author="Аня", rating=rating, body="текст")


def test_rejects_bool_as_rating(reviews):
    """`True` в Python — это `int`, и наивная проверка его пропустит.

    Дальше он превратится в оценку «1», и никто не поймёт, откуда
    она взялась. Поэтому bool отсекается явно.
    """
    with pytest.raises(ValidationError, match="целым числом"):
        reviews.add(author="Аня", rating=True, body="текст")


def test_rejects_too_long_body(reviews):
    with pytest.raises(ValidationError, match="длиннее"):
        reviews.add(author="Аня", rating=5, body="я" * 4001)


def test_collects_all_problems_at_once(reviews):
    """Все ошибки сразу, а не по одной за запрос.

    Тот же принцип, что в загрузчике каталога: чинить форму
    по одной ошибке за отправку — это издевательство.
    """
    with pytest.raises(ValidationError) as info:
        reviews.add(author="", rating=9, body="")
    message = info.value.message
    assert "имя" in message and "оценка" in message and "текст" in message


# ---------- хранение и связи ----------

def test_saves_and_reads_back(reviews):
    reviews.add(author="  Аня  ", rating=4, body="  нормально  ", title=" Итог ")
    row = reviews.list_recent()[0]
    assert row["author"] == "Аня", "пробелы по краям должны срезаться"
    assert row["body"] == "нормально"
    assert row["title"] == "Итог"
    assert row["rating"] == 4


def test_review_links_to_product(reviews, products):
    product = products.add("SKU-1", "Игра", 100000, 5)
    reviews.add(author="Аня", rating=5, body="огонь", product_id=product.id)

    row = reviews.list_recent()[0]

    assert row["product_id"] == product.id
    assert row["product_title"] == "Игра", "название приходит джойном, без второго запроса"


def test_review_about_shop_has_no_product(reviews):
    """LEFT JOIN, а не INNER: отзыв о магазине не привязан к товару.

    С INNER JOIN такой отзыв просто исчез бы из выдачи — молча,
    без единой ошибки.
    """
    reviews.add(author="Аня", rating=5, body="хороший магазин")
    row = reviews.list_recent()[0]
    assert row["product_id"] is None
    assert row["product_title"] is None


def test_listing_does_not_do_n_plus_one(reviews, products, db):
    """Названия товаров не должны стоить по запросу на отзыв."""
    for i in range(10):
        product = products.add(f"SKU-{i}", f"Игра {i}", 1000, 5)
        reviews.add(author=f"Автор {i}", rating=5, body="текст", product_id=product.id)

    with db.counter.measure():
        rows = reviews.list_recent()

    assert len(rows) == 10
    assert db.counter.count == 1, f"ожидался один запрос, сделано {db.counter.count}"


def test_newest_first(reviews):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    reviews.add(author="Старый", rating=5, body="раньше", created_at=now - timedelta(days=10))
    reviews.add(author="Новый", rating=5, body="позже", created_at=now)

    assert [r["author"] for r in reviews.list_recent()] == ["Новый", "Старый"]


# ---------- сводка ----------

def test_summary_counts_stars(reviews):
    for rating in [5, 5, 5, 4, 2]:
        reviews.add(author="Кто-то", rating=rating, body="текст")

    summary = reviews.summary()

    assert summary["total"] == 5
    assert round(float(summary["average"]), 2) == 4.2
    assert summary["stars5"] == 3
    assert summary["stars4"] == 1
    assert summary["stars2"] == 1
    assert summary["stars1"] == 0


def test_summary_on_empty_table(reviews):
    """Пустая таблица не должна давать деления на ноль или None."""
    summary = reviews.summary()
    assert summary["total"] == 0
    assert float(summary["average"]) == 0


# ---------- демонстрационные данные ----------

def test_demo_flag_is_stored_and_visible(reviews):
    reviews.add(author="Демо", rating=5, body="выдуманный", is_demo=True)
    reviews.add(author="Живой", rating=5, body="настоящий", is_demo=False)

    by_author = {r["author"]: r for r in reviews.list_recent()}

    assert by_author["Демо"]["is_demo"]
    assert not by_author["Живой"]["is_demo"]


def test_demo_reviews_can_be_wiped_without_touching_real_ones(reviews):
    reviews.add(author="Демо", rating=5, body="выдуманный", is_demo=True)
    reviews.add(author="Живой", rating=4, body="настоящий")

    reviews.delete_demo()

    remaining = reviews.list_recent()
    assert [r["author"] for r in remaining] == ["Живой"]


def test_seeding_twice_does_not_duplicate(reviews, products):
    """Повторный `seed` не должен плодить копии демо-отзывов."""
    products.add("SKU-1", "Игра", 1000, 5)
    items = [ReviewItem(author="Демо", rating=5, body="текст", sku="SKU-1")]

    seed_reviews(reviews, products, items)
    seed_reviews(reviews, products, items)

    assert len(reviews.list_recent()) == 1


def test_seeding_keeps_review_with_unknown_sku(reviews, products):
    """Опечатка в артикуле не должна стоить человеку его текста."""
    items = [ReviewItem(author="Демо", rating=5, body="текст", sku="НЕТ-ТАКОГО")]

    seed_reviews(reviews, products, items)

    row = reviews.list_recent()[0]
    assert row["body"] == "текст"
    assert row["product_id"] is None


def test_seeding_spreads_dates(reviews, products):
    """Все отзывы одним моментом — сразу видно, что данные сгенерированы."""
    items = [ReviewItem(author=f"А{i}", rating=5, body="текст") for i in range(5)]

    seed_reviews(reviews, products, items)

    dates = {r["created_at"][:10] for r in reviews.list_recent()}
    assert len(dates) == 5, "у каждого отзыва должна быть своя дата"


# ---------- интерфейсы двух баз ----------

def test_review_repositories_agree_on_signatures():
    """Та же проверка, что и у товаров, и по той же причине."""
    pytest.importorskip("psycopg", reason="psycopg не установлен")
    from shopapi.repositories.reviews_repo import PgReviewRepository

    for name in ["add", "list_recent", "summary", "get", "delete_demo"]:
        ours = inspect.signature(getattr(SqliteReviewRepository, name))
        theirs = inspect.signature(getattr(PgReviewRepository, name))
        assert list(ours.parameters) == list(theirs.parameters), name
