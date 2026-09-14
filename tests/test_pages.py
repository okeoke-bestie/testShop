"""Маршруты страниц витрины.

Тест здесь ровно про одно, но важное. Разделы сайта переключаются
в браузере через History API: адрес меняется на /reviews, запроса
к серверу нет. Всё выглядит работающим — пока человек не нажмёт F5
или не откроет присланную ссылку. Тогда браузер спросит `/reviews`
у сервера по-настоящему, и без обработчика ответом будет 404.

Поломка неприятна тем, что не воспроизводится при обычном хождении
по сайту: её находит пользователь, а не разработчик. Поэтому она
закрыта тестом, а не памятью.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI не установлен")
from fastapi.testclient import TestClient  # noqa: E402

from shopapi import api  # noqa: E402

PAGES = ["/", "/reviews", "/delivery", "/contacts", "/about"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Клиент на своей временной базе.

    DATABASE_URL снимается явно: иначе тест на машине разработчика
    пойдёт в его настоящий PostgreSQL и станет зависеть от чужих данных.
    """
    # Пустая строка, а не удаление переменной: фабрика читает ещё и файл
    # .env, и удалённая переменная просто вернулась бы оттуда. На этом
    # уже обжёгся прогон тестов, который ушёл в настоящую базу
    # и вычистил в ней каталог.
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SHOP_SQLITE_PATH", str(tmp_path / "pages.sqlite3"))
    with TestClient(api.app) as client:
        yield client


@pytest.mark.parametrize("path", PAGES)
def test_page_opens_directly(client, path):
    """Прямой заход по адресу раздела отдаёт страницу, а не 404."""
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # Отдаётся одна и та же оболочка: раздел выбирает роутер в браузере.
    assert 'data-page="reviews"' in response.text


def test_unknown_page_is_404(client):
    """Отдавать index.html на что угодно нельзя.

    Иначе опечатка в ссылке молча покажет главную, поисковик
    проиндексирует десяток дублей одной страницы, а сломанную ссылку
    никто никогда не заметит.
    """
    response = client.get("/такой-страницы-нет")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_api_paths_are_not_swallowed_by_page_route(client):
    """Ручка `/{page}` не должна перехватывать `/api/...` и `/docs`."""
    assert client.get("/api/products").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_catalog_answers_with_platforms(client):
    payload = client.get("/api/products").json()
    assert "platforms" in payload
    assert "categories" in payload


def test_review_can_be_posted_and_read_back(client):
    created = client.post("/api/reviews", json={
        "author": "Аня", "rating": 5, "body": "всё пришло вовремя",
    })
    assert created.status_code == 201

    payload = client.get("/api/reviews").json()
    assert payload["summary"]["total"] == 1
    review = payload["items"][0]
    assert review["author"] == "Аня"
    assert review["is_demo"] is False, "отзыв с сайта не должен помечаться как демо"


def test_bad_review_gets_unified_error(client):
    """Ошибка валидации приходит в том же формате, что доменные."""
    response = client.post("/api/reviews", json={
        "author": "Аня", "rating": 99, "body": "текст",
    })
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "validation_error"
    assert "rating" in payload["message"]


def test_product_answer_carries_cover_path(client):
    """У каждого товара есть путь к обложке, и он ведёт в статику."""
    payload = client.get("/api/products").json()
    for item in payload["items"]:
        assert item["art"] == f"/static/art/{item['sku']}.svg"


# ---------- страница товара ----------

def _seed(client, sku="SKU-1", stock=5):
    """Кладёт товар прямо через бэкенд приложения.

    Через API товар не создать намеренно: каталог наполняется файлом
    и командой seed, а не запросами с витрины.
    """
    from shopapi import api as api_module

    backend = api_module.STATE["backend"]
    return backend.products.add(sku, "Тестовая игра", 100000, stock,
                                "описание", "Игры", "💿", "PlayStation 5",
                                "Боевик", "Студия", 2020,
                                '{"Носитель": "диск", "Возраст": "16+"}')


def test_product_page_returns_everything_at_once(client):
    _seed(client)
    payload = client.get("/api/products/by-sku/SKU-1").json()

    assert payload["product"]["title"] == "Тестовая игра"
    assert payload["specs"]["Носитель"] == "диск"
    assert payload["reviews"] == []
    assert payload["rating"]["total"] == 0
    assert payload["can_review"] is False
    assert "recommendations" in payload


def test_unknown_sku_is_404(client):
    assert client.get("/api/products/by-sku/НЕТ-ТАКОГО").status_code == 404


def test_product_url_serves_the_page(client):
    """Прямой заход на /product/SKU отдаёт витрину, а не 404."""
    response = client.get("/product/SKU-1")
    assert response.status_code == 200
    assert 'data-page="product"' in response.text


def test_broken_specs_do_not_break_the_page(client):
    """Битый JSON в характеристиках не должен ронять весь раздел."""
    from shopapi import api as api_module

    backend = api_module.STATE["backend"]
    backend.products.add("SKU-BAD", "Товар", 1000, 1, "", "Игры", "💿",
                         "PC", "Жанр", "Студия", 2020, "{это не json")

    payload = client.get("/api/products/by-sku/SKU-BAD").json()
    assert payload["specs"] == {}


def test_recommendations_exclude_the_product_itself(client):
    _seed(client, "SKU-1")
    _seed(client, "SKU-2")
    payload = client.get("/api/products/by-sku/SKU-1").json()
    assert all(item["sku"] != "SKU-1" for item in payload["recommendations"])


# ---------- отзыв только после покупки ----------

def test_review_without_customer_is_401(client):
    product = _seed(client)
    response = client.post("/api/reviews", json={
        "author": "Гость", "rating": 5, "body": "текст", "product_id": product.id,
    })
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def _register(client, email="a@example.com", name="Аня") -> dict:
    """Регистрирует покупателя и возвращает заголовок с токеном."""
    payload = client.post("/api/auth/register", json={
        "name": name, "email": email, "password": "dlinnyy-parol-123",
    }).json()
    return {"Authorization": f"Bearer {payload['token']}"}


def test_review_without_purchase_is_403(client):
    product = _seed(client)
    auth = _register(client)

    response = client.post(
        "/api/reviews",
        json={"author": "Аня", "rating": 5, "body": "текст", "product_id": product.id},
        headers=auth,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_review_after_purchase_is_accepted_and_verified(client):
    product = _seed(client)
    auth = _register(client)

    client.post("/api/orders", headers=auth, json={
        "items": [{"product_id": product.id, "quantity": 1}],
    })

    response = client.post(
        "/api/reviews",
        json={"author": "Аня", "rating": 5, "body": "купила и довольна",
              "product_id": product.id},
        headers=auth,
    )

    assert response.status_code == 201
    assert response.json()["verified"] is True

    shown = client.get("/api/products/by-sku/SKU-1").json()
    assert shown["reviews"][0]["verified"] is True


def test_second_review_on_same_product_is_409(client):
    product = _seed(client)
    headers = _register(client)
    client.post("/api/orders", headers=headers, json={
        "items": [{"product_id": product.id, "quantity": 2}],
    })
    body = {"author": "Аня", "rating": 5, "body": "текст", "product_id": product.id}

    assert client.post("/api/reviews", json=body, headers=headers).status_code == 201
    second = client.post("/api/reviews", json=body, headers=headers)

    assert second.status_code == 409
    assert second.json()["code"] == "duplicate_review"


def test_review_about_shop_needs_no_purchase(client):
    """Отзыв о магазине целиком покупки не требует."""
    response = client.post("/api/reviews", json={
        "author": "Гость", "rating": 5, "body": "хороший магазин",
    })
    assert response.status_code == 201
    assert response.json()["verified"] is False


# ---------- профиль ----------

def test_profile_requires_a_customer(client):
    assert client.get("/api/customers/me").status_code == 401


def test_profile_shows_orders_and_purchases(client):
    product = _seed(client)
    auth = _register(client)
    client.post("/api/orders", headers=auth, json={
        "items": [{"product_id": product.id, "quantity": 1}],
    })

    payload = client.get("/api/customers/me", headers=auth).json()

    assert payload["profile"]["name"] == "Аня"
    assert len(payload["orders"]) == 1
    assert payload["orders"][0]["total_kopecks"] == 100000
    assert len(payload["purchased"]) == 1
    assert payload["purchased"][0]["reviewed"] is False


def test_catalog_carries_ratings_without_n_plus_one(client):
    """Рейтинги в каталоге берутся одним запросом на весь список."""
    from shopapi import api as api_module

    backend = api_module.STATE["backend"]
    for i in range(5):
        product = backend.products.add(f"R-{i}", f"Товар {i}", 1000, 5)
        backend.reviews.add(author="Кто-то", rating=5, body="текст",
                            product_id=product.id)

    with backend.db.counter.measure():
        payload = client.get("/api/products").json()

    assert all("rating" in item for item in payload["items"])
    # Каталог + категории + платформы + рейтинги = четыре запроса,
    # и это число НЕ растёт с числом товаров.
    assert backend.db.counter.count <= 5, backend.db.counter.statements


# ---------- вход через HTTP ----------

def test_register_returns_token_and_profile(client):
    response = client.post("/api/auth/register", json={
        "name": "Даниил", "email": "d@example.com",
        "password": "dlinnyy-parol-123", "city": "Москва",
    })
    assert response.status_code == 201
    payload = response.json()
    assert payload["token"]
    assert payload["profile"]["name"] == "Даниил"


def test_response_never_carries_password_hash(client):
    """Самый простой способ опубликовать хеш — отдать строку из базы
    целиком, «чтобы не перечислять поля». Проверяем, что не отдаём."""
    token = client.post("/api/auth/register", json={
        "name": "Аня", "email": "a@example.com", "password": "dlinnyy-parol-123",
    }).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    for path in ["/api/auth/me", "/api/customers/me"]:
        body = client.get(path, headers=auth).text
        assert "password" not in body, path
        assert "scrypt" not in body, path


def test_login_flow(client):
    client.post("/api/auth/register", json={
        "name": "Аня", "email": "a@example.com", "password": "dlinnyy-parol-123",
    })
    response = client.post("/api/auth/login", json={
        "email": "a@example.com", "password": "dlinnyy-parol-123",
    })
    assert response.status_code == 200
    assert response.json()["profile"]["email"] == "a@example.com"


def test_login_with_wrong_password_is_401(client):
    client.post("/api/auth/register", json={
        "name": "Аня", "email": "a@example.com", "password": "dlinnyy-parol-123",
    })
    response = client.post("/api/auth/login", json={
        "email": "a@example.com", "password": "ne-tot-parol",
    })
    assert response.status_code == 401
    assert response.json()["code"] == "bad_credentials"


def test_me_without_token_is_not_an_error(client):
    """Анонимный посетитель — обычное состояние, а не сбой."""
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["profile"] is None


def test_garbage_token_is_treated_as_anonymous(client):
    response = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.json()["profile"] is None


def test_wrong_scheme_is_ignored(client):
    """Basic вместо Bearer — не наша схема, токен не разбирается."""
    token = client.post("/api/auth/register", json={
        "name": "Аня", "email": "a@example.com", "password": "dlinnyy-parol-123",
    }).json()["token"]
    response = client.get("/api/auth/me", headers={"Authorization": f"Basic {token}"})
    assert response.json()["profile"] is None


def test_logout_invalidates_the_token(client):
    token = client.post("/api/auth/register", json={
        "name": "Аня", "email": "a@example.com", "password": "dlinnyy-parol-123",
    }).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    assert client.post("/api/auth/logout", headers=auth).status_code == 204
    assert client.get("/api/auth/me", headers=auth).json()["profile"] is None


def test_customer_list_endpoint_is_gone(client):
    """Ручка со списком всех покупателей удалена: она отдавала
    персональные данные любому посетителю."""
    assert client.get("/api/customers").status_code == 404


# ---------- заказы принадлежат вошедшему ----------

def test_order_requires_login(client):
    product = _seed(client)
    response = client.post("/api/orders", json={
        "items": [{"product_id": product.id, "quantity": 1}],
    })
    assert response.status_code == 401


def test_order_ignores_customer_id_from_body(client):
    """Покупатель берётся из сессии. Число в теле запроса ни на что
    не влияет — иначе заказ оформлялся бы от чужого имени."""
    product = _seed(client)
    auth = _register(client)

    client.post("/api/orders", headers=auth, json={
        "customer_id": 99999,
        "items": [{"product_id": product.id, "quantity": 1}],
    })

    payload = client.get("/api/customers/me", headers=auth).json()
    assert len(payload["orders"]) == 1, "заказ должен принадлежать вошедшему"


def test_cannot_cancel_someone_elses_order(client):
    """Небезопасная прямая ссылка на объект: без проверки владельца
    отмена чужого заказа — это перебор чисел в адресе."""
    product = _seed(client, stock=10)
    first = _register(client, "one@example.com", "Первый")
    second = _register(client, "two@example.com", "Второй")

    order_id = client.post("/api/orders", headers=first, json={
        "items": [{"product_id": product.id, "quantity": 1}],
    }).json()["id"]

    stranger = client.post(f"/api/orders/{order_id}/cancel", headers=second)
    # 404, а не 403: иначе по коду ответа перебором узнаются
    # существующие номера заказов.
    assert stranger.status_code == 404

    owner = client.post(f"/api/orders/{order_id}/cancel", headers=first)
    assert owner.status_code == 200


# ---------- кэш ----------

def test_static_is_served_without_cache(client):
    """Обновил страницу — получил свежие css и js, без Ctrl+Shift+R."""
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")


def test_html_is_not_cached_and_carries_version(client):
    """Оболочка не кэшируется: иначе браузер оставил бы у себя старую
    страницу со старой версией в ссылках, и версия не помогла бы."""
    response = client.get("/")
    assert "no-cache" in response.headers.get("cache-control", "")
    assert f"app.css?v={api.ASSET_VERSION}" in response.text
    assert "__ASSET_VERSION__" not in response.text, "плейсхолдер должен быть заменён"
