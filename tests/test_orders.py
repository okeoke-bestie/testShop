"""Тесты бизнес-логики заказов."""

import pytest

from shopapi.errors import (
    IdempotencyConflict,
    NotFound,
    OutOfStock,
    ValidationError,
)
from shopapi.models import OrderStatus
from shopapi.services.orders import OrderRequest

# ---------- валидация ----------


def test_empty_order_rejected(service):
    with pytest.raises(ValidationError):
        service.create_order(OrderRequest(customer_id=1, items=[]))


def test_negative_quantity_rejected(service, sample_products):
    with pytest.raises(ValidationError):
        service.create_order(
            OrderRequest(customer_id=1, items=[(sample_products["game"].id, -1)])
        )


def test_duplicate_product_rejected(service, sample_products):
    """Дубли запрещены не из вредности: иначе обходится лимит
    количества — две строки по 100 штук вместо одной по 200."""
    game_id = sample_products["game"].id
    with pytest.raises(ValidationError):
        service.create_order(OrderRequest(customer_id=1, items=[(game_id, 1), (game_id, 1)]))


def test_too_many_lines_rejected(service):
    with pytest.raises(ValidationError):
        service.create_order(
            OrderRequest(customer_id=1, items=[(i, 1) for i in range(1, 60)])
        )


def test_validation_happens_before_database(service, db):
    """Некорректный запрос не должен доходить до базы.

    Проверяется счётчиком запросов: открывать транзакцию ради заведомо
    неверного запроса — впустую занятое соединение.
    """
    with db.counter.measure(), pytest.raises(ValidationError):
        service.create_order(OrderRequest(customer_id=1, items=[]))
    assert db.counter.count == 0


# ---------- создание ----------


def test_creates_order_and_reserves_stock(service, products, sample_products):
    game = sample_products["game"]

    order = service.create_order(OrderRequest(customer_id=42, items=[(game.id, 2)]))

    assert order.id is not None
    assert order.status is OrderStatus.NEW
    assert order.total_kopecks == 2 * game.price_kopecks
    assert products.get(game.id).stock == 1


def test_price_is_frozen_at_order_time(service, products, db, sample_products):
    """Цена копируется в заказ и не меняется вслед за каталогом.

    Иначе вчерашние заказы пересчитались бы при изменении прайса —
    и отчёты за прошлый месяц перестали бы сходиться.
    """
    game = sample_products["game"]
    order = service.create_order(OrderRequest(customer_id=1, items=[(game.id, 1)]))
    original_total = order.total_kopecks

    db.execute("UPDATE products SET price_kopecks = 999999 WHERE id = ?", (game.id,))

    from shopapi.repositories.sqlite_repo import SqliteOrderRepository

    reloaded = SqliteOrderRepository(db).get(order.id)
    assert reloaded.total_kopecks == original_total


def test_unknown_product_reports_all_missing(service):
    with pytest.raises(NotFound) as info:
        service.create_order(OrderRequest(customer_id=1, items=[(404, 1), (405, 1)]))
    # Сообщать обо ВСЕХ отсутствующих сразу, а не о первом: иначе клиент
    # будет чинить свой запрос по одной позиции за попытку.
    assert set(info.value.details["product_ids"]) == {404, 405}


def test_out_of_stock_reports_available(service, sample_products):
    rare = sample_products["rare"]
    with pytest.raises(OutOfStock) as info:
        service.create_order(OrderRequest(customer_id=1, items=[(rare.id, 5)]))

    assert info.value.details["requested"] == 5
    assert info.value.details["available"] == 1
    assert info.value.http_status == 409


# ---------- идемпотентность ----------


def test_repeat_with_same_key_returns_same_order(service, products, sample_products):
    game = sample_products["game"]
    request = OrderRequest(
        customer_id=1, items=[(game.id, 1)], idempotency_key="order-123"
    )

    first = service.create_order(request)
    second = service.create_order(request)

    assert first.id == second.id
    # Товар списан ОДИН раз, а не два.
    assert products.get(game.id).stock == 2


def test_same_key_different_payload_is_rejected(service, sample_products):
    """Молча вернуть старый заказ здесь нельзя.

    Клиент попросил другое, а получил бы прежнее — и не заметил.
    Явная ошибка заставляет его исправить ключ.
    """
    game = sample_products["game"]
    console = sample_products["console"]
    service.create_order(
        OrderRequest(customer_id=1, items=[(game.id, 1)], idempotency_key="key-1")
    )

    with pytest.raises(IdempotencyConflict):
        service.create_order(
            OrderRequest(customer_id=1, items=[(console.id, 1)], idempotency_key="key-1")
        )


def test_fingerprint_ignores_item_order(sample_products):
    """[(1,2),(3,4)] и [(3,4),(1,2)] — один и тот же заказ."""
    a = OrderRequest(customer_id=1, items=[(1, 2), (3, 4)])
    b = OrderRequest(customer_id=1, items=[(3, 4), (1, 2)])

    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_differs_for_different_customer():
    a = OrderRequest(customer_id=1, items=[(1, 2)])
    b = OrderRequest(customer_id=2, items=[(1, 2)])

    assert a.fingerprint() != b.fingerprint()


def test_order_without_key_is_not_deduplicated(service, products, sample_products):
    """Без ключа два одинаковых запроса — два разных заказа.

    Это правильно: клиент действительно мог захотеть купить дважды.
    Дедупликация без явного ключа означала бы потерю законных заказов.
    """
    console = sample_products["console"]
    first = service.create_order(OrderRequest(customer_id=1, items=[(console.id, 1)]))
    second = service.create_order(OrderRequest(customer_id=1, items=[(console.id, 1)]))

    assert first.id != second.id
    assert products.get(console.id).stock == 8


# ---------- отмена ----------


def test_cancel_returns_stock(service, products, sample_products):
    game = sample_products["game"]
    order = service.create_order(OrderRequest(customer_id=1, items=[(game.id, 2)]))
    assert products.get(game.id).stock == 1

    cancelled = service.cancel_order(order.id)

    assert cancelled.status is OrderStatus.CANCELLED
    assert products.get(game.id).stock == 3


def test_cancel_twice_is_rejected(service, products, sample_products):
    """Вторая отмена не должна вернуть остаток повторно."""
    game = sample_products["game"]
    order = service.create_order(OrderRequest(customer_id=1, items=[(game.id, 2)]))
    service.cancel_order(order.id)

    with pytest.raises(ValidationError):
        service.cancel_order(order.id)

    assert products.get(game.id).stock == 3


def test_cancel_unknown_order(service):
    with pytest.raises(NotFound):
        service.cancel_order(999)


# ---------- деньги ----------


def test_money_is_integer_kopecks(sample_products):
    """Деньги в копейках целым числом.

    float для денег не годится: 0.1 + 0.2 != 0.3, и на тысячах операций
    расхождение попадает в отчёты.
    """
    console = sample_products["console"]
    assert isinstance(console.price_kopecks, int)
    assert console.price_rub == "49990.00"


def test_negative_price_rejected_by_model():
    from shopapi.models import Product

    with pytest.raises(ValueError):
        Product(id=1, sku="X", title="X", price_kopecks=-1, stock=0)
