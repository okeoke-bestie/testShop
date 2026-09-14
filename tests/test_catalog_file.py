"""Тесты загрузки каталога из файла.

Файл правит человек, поэтому ошибки в нём — нормальная ситуация,
а не исключение. Проверяется, что каждая ошибка называет позицию
и поле, а не падает где-то в глубине с KeyError.
"""

import json

import pytest

from shopapi.catalog_file import CatalogItem, load_catalog, rub_to_kopecks, seed_database
from shopapi.errors import ValidationError


def write(tmp_path, payload) -> str:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ---------- цены ----------


def test_rubles_convert_to_kopecks():
    assert rub_to_kopecks(100) == 10000
    assert rub_to_kopecks(0) == 0


def test_kopecks_are_not_lost_on_float():
    """Классическая ловушка: 4999.90 * 100 = 499989.99999999994.

    Через int() это дало бы 499989 — на копейку меньше. Поэтому
    пересчёт идёт через Decimal.
    """
    assert rub_to_kopecks(4999.90) == 499990
    assert rub_to_kopecks(5990.50) == 599050
    assert rub_to_kopecks(0.1) + rub_to_kopecks(0.2) == rub_to_kopecks(0.3)


def test_negative_price_rejected():
    with pytest.raises(ValidationError):
        rub_to_kopecks(-1)


def test_garbage_price_rejected():
    with pytest.raises(ValidationError):
        rub_to_kopecks("дорого")


# ---------- чтение файла ----------


def test_loads_valid_catalog(tmp_path):
    path = write(tmp_path, {"products": [
        {"sku": "A-1", "title": "Товар", "price_rub": 12.34, "stock": 5},
    ]})

    items = load_catalog(path)

    assert items == [CatalogItem(sku="A-1", title="Товар", price_kopecks=1234, stock=5)]


def test_accepts_plain_list(tmp_path):
    """Файл может быть просто списком — так удобнее писать руками."""
    path = write(tmp_path, [{"sku": "A-1", "title": "Т", "price_rub": 1, "stock": 1}])

    assert len(load_catalog(path)) == 1


def test_missing_file_reports_path(tmp_path):
    with pytest.raises(ValidationError) as info:
        load_catalog(tmp_path / "нет-такого.json")
    assert "не найден" in info.value.message


def test_broken_json_reports_line(tmp_path):
    """Сообщение должно называть строку — иначе человек ищет опечатку
    в файле на сто позиций вручную."""
    path = tmp_path / "catalog.json"
    path.write_text('{"products": [{"sku": "A",,}]}', encoding="utf-8")

    with pytest.raises(ValidationError) as info:
        load_catalog(path)

    assert "строка" in info.value.message


def test_all_problems_reported_at_once(tmp_path):
    """Ошибки собираются все сразу.

    Иначе человек чинит файл по одной строке за запуск — то же
    соображение, по которому сервис сообщает обо всех отсутствующих
    товарах заказа сразу.
    """
    path = write(tmp_path, {"products": [
        {"sku": "", "title": "Без артикула", "price_rub": 1, "stock": 1},
        {"sku": "B-2", "title": "", "price_rub": 1, "stock": 1},
        {"sku": "C-3", "title": "Минус", "price_rub": 1, "stock": -5},
    ]})

    with pytest.raises(ValidationError) as info:
        load_catalog(path)

    assert info.value.details["count"] == 3
    assert "sku" in info.value.message
    assert "title" in info.value.message
    assert "stock" in info.value.message


def test_duplicate_sku_reported(tmp_path):
    """Дубль упал бы на уникальном индексе в базе, но сообщение оттуда
    человеку ничего не скажет."""
    path = write(tmp_path, {"products": [
        {"sku": "A-1", "title": "Первый", "price_rub": 1, "stock": 1},
        {"sku": "A-1", "title": "Второй", "price_rub": 1, "stock": 1},
    ]})

    with pytest.raises(ValidationError) as info:
        load_catalog(path)

    assert "повторяется" in info.value.message


def test_stock_must_be_integer(tmp_path):
    path = write(tmp_path, [{"sku": "A", "title": "Т", "price_rub": 1, "stock": 2.5}])

    with pytest.raises(ValidationError) as info:
        load_catalog(path)

    assert "целым числом" in info.value.message


def test_boolean_is_not_accepted_as_stock(tmp_path):
    """В Python True — это 1, и без явной проверки `stock: true`
    молча превратился бы в одну штуку."""
    path = write(tmp_path, [{"sku": "A", "title": "Т", "price_rub": 1, "stock": True}])

    with pytest.raises(ValidationError):
        load_catalog(path)


def test_empty_catalog_rejected(tmp_path):
    with pytest.raises(ValidationError):
        load_catalog(write(tmp_path, {"products": []}))


def test_comment_keys_are_ignored(tmp_path):
    """В файле есть поле с пояснением для человека — оно не должно мешать."""
    path = write(tmp_path, {
        "_комментарий": ["как править этот файл"],
        "products": [{"sku": "A", "title": "Т", "price_rub": 1, "stock": 1}],
    })

    assert len(load_catalog(path)) == 1


# ---------- заливка в базу ----------


def test_seed_puts_items_into_database(db, products):
    items = [
        CatalogItem("A-1", "Первый", 10000, 5),
        CatalogItem("A-2", "Второй", 20000, 3),
    ]

    count = seed_database(products, items)

    assert count == 2
    assert products.db.query_one("SELECT COUNT(*) AS n FROM products")["n"] == 2


def test_seed_replaces_old_catalog(db, products):
    """Файл — источник истины: удалённая из него позиция должна исчезнуть
    и из базы, иначе она останется там навсегда."""
    products.add("OLD-1", "Устаревший", 100, 1)

    seed_database(products, [CatalogItem("NEW-1", "Новый", 200, 2)])

    rows = products.db.query_all("SELECT sku FROM products")
    assert [r["sku"] for r in rows] == ["NEW-1"]


def test_seed_refuses_when_orders_exist(db, products, service):
    """Чистка каталога при существующих заказах сломала бы историю:
    у строк заказа внешние ключи на товары."""
    from shopapi.services.orders import OrderRequest

    product = products.add("A-1", "Товар", 10000, 10)
    service.create_order(OrderRequest(customer_id=1, items=[(product.id, 1)]))

    with pytest.raises(ValidationError) as info:
        seed_database(products, [CatalogItem("B-1", "Другой", 100, 1)])

    assert "заказы" in info.value.message


def test_bundled_catalog_file_is_valid():
    """Каталог, который идёт в комплекте, должен читаться без ошибок."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "data" / "catalog.json"
    items = load_catalog(path)

    assert len(items) >= 5
    assert all(item.price_kopecks > 0 for item in items)
    assert len({item.sku for item in items}) == len(items)
