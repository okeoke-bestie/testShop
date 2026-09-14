"""Готовность к публичному размещению.

Отдельный файл, потому что ошибки этого рода не ловятся обычными
тестами: приложение работает, тесты зелёные, а на хостинге сервис
не поднимается или поднимается с пустой витриной. Причина каждый раз
одна и та же — окружение там не такое, как на своей машине: порт
назначает хостинг, база чистая, клиент приходит через прокси.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import main as cli
from shopapi import bootstrap
from shopapi.catalog_file import load_catalog

ROOT = Path(__file__).resolve().parents[1]


# ---------- порт ----------

def test_port_comes_from_hosting_variable(monkeypatch):
    """Хостинг сообщает порт переменной PORT и ждёт, что его послушают.

    Сервис, севший на свой любимый порт, хостинг просто не найдёт:
    проверка живости не пройдёт, и деплой будет отменён.
    """
    monkeypatch.delenv("SHOP_PORT", raising=False)
    monkeypatch.setenv("PORT", "10000")
    assert cli.resolve_port(None) == 10000


def test_own_variable_wins_over_generic_one(monkeypatch):
    """SHOP_PORT главнее PORT.

    PORT — слишком общее имя: оно может оказаться в окружении от чего
    угодно постороннего. Настройка проекта не должна молча уступать
    случайной переменной.
    """
    monkeypatch.setenv("SHOP_PORT", "8080")
    monkeypatch.setenv("PORT", "10000")
    assert cli.resolve_port(None) == 8080


def test_flag_wins_over_everything(monkeypatch):
    monkeypatch.setenv("SHOP_PORT", "8080")
    monkeypatch.setenv("PORT", "10000")
    assert cli.resolve_port(3000) == 3000


def test_garbage_in_port_does_not_crash_startup(monkeypatch):
    monkeypatch.delenv("SHOP_PORT", raising=False)
    monkeypatch.setenv("PORT", "не число")
    assert cli.resolve_port(None) == cli.DEFAULT_PORT


# ---------- наполнение базы ----------

@pytest.fixture
def backend(tmp_path, monkeypatch):
    """Настоящий бэкенд на временной sqlite-базе."""
    monkeypatch.setenv("DATABASE_URL", "")
    from shopapi.factory import make_backend

    made = make_backend(sqlite_path=str(tmp_path / "deploy.sqlite3"))
    yield made
    made.close()


def test_seed_fills_an_empty_catalog(backend):
    assert bootstrap.catalog_is_empty(backend)
    loaded = bootstrap.seed_if_empty(backend)
    assert loaded > 0
    assert not bootstrap.catalog_is_empty(backend)


def test_seed_does_not_touch_a_filled_catalog(backend):
    """Главное свойство: повторный запуск ничего не стирает.

    На бесплатном тарифе контейнер перезапускается сам — после простоя,
    после передеплоя, просто так. Если наполнение работает каждый раз,
    заказы и отзывы посетителей исчезают при каждом пробуждении сайта.
    """
    bootstrap.seed_if_empty(backend)
    backend.products.db.execute("UPDATE products SET stock = 0")

    assert bootstrap.seed_if_empty(backend) == 0
    row = backend.db.query_one("SELECT SUM(stock) AS total FROM products")
    assert row["total"] == 0  # остатки остались нулевыми, каталог не перезалили


def test_seed_brings_demo_reviews(backend):
    bootstrap.seed_if_empty(backend)
    row = backend.db.query_one("SELECT COUNT(*) AS n FROM reviews")
    assert row["n"] > 100


# ---------- поддержание демо ----------

def test_restock_returns_sold_out_items(backend):
    bootstrap.seed_if_empty(backend)
    backend.products.db.execute("UPDATE products SET stock = 0")

    restored = bootstrap.restock(backend)
    assert restored > 0

    baseline = {item.sku: item.stock for item in load_catalog(bootstrap.CATALOG_FILE)}
    rows = backend.db.query_all("SELECT sku, stock FROM products")
    assert all(row["stock"] == baseline[row["sku"]] for row in rows)


def test_restock_does_not_lower_raised_stock(backend):
    """Поднимаются только просевшие позиции.

    Если остаток больше, чем в файле, — значит, его меняли осознанно,
    и фоновая уборка не имеет права это затирать.
    """
    bootstrap.seed_if_empty(backend)
    backend.products.db.execute("UPDATE products SET stock = 9999 WHERE sku = 'PS5-GOW18'")

    bootstrap.restock(backend)
    row = backend.db.query_one("SELECT stock FROM products WHERE sku = 'PS5-GOW18'")
    assert row["stock"] == 9999


def test_restock_keeps_orders(backend):
    """Остатки возвращаются, история покупок — нет.

    Иначе вместе с товаром у человека пропало бы право оставить отзыв,
    который он уже заслужил покупкой.
    """
    bootstrap.seed_if_empty(backend)
    product = backend.db.query_one("SELECT id FROM products WHERE stock > 0 LIMIT 1")
    customer = backend.customers.register(
        name="Покупатель", email="buyer@example.com", password="parol-na-vosem",
    )
    from shopapi.services.orders import OrderRequest

    order = backend.order_service.create_order(
        OrderRequest(customer_id=customer, items=[(product["id"], 1)]),
    )

    bootstrap.restock(backend)

    assert backend.customers.has_purchased(customer, product["id"])
    assert backend.orders.get(order.id) is not None


def test_prepare_is_off_without_the_flag(backend, monkeypatch):
    """Без SHOP_AUTO_SEED сервис в базу не пишет.

    На своей машине базу наполняет человек; приложение, которое молча
    заливает в неё демо-данные при каждом запуске, — сюрприз, которого
    никто не просил.
    """
    monkeypatch.delenv("SHOP_AUTO_SEED", raising=False)
    bootstrap.prepare(backend)
    assert bootstrap.catalog_is_empty(backend)


def test_prepare_seeds_with_the_flag(backend, monkeypatch):
    monkeypatch.setenv("SHOP_AUTO_SEED", "1")
    bootstrap.prepare(backend)
    assert not bootstrap.catalog_is_empty(backend)


def test_prepare_survives_a_broken_catalog(backend, monkeypatch, tmp_path):
    """Сломанный файл данных не должен ронять сервис.

    Витрина без каталога — плохо, но это страница с понятной ошибкой
    и живым API. Упавший процесс — это страница хостинга «сервис
    не запустился», по которой не видно вообще ничего.
    """
    broken = tmp_path / "broken.json"
    broken.write_text("{ это не json", encoding="utf-8")
    monkeypatch.setenv("SHOP_AUTO_SEED", "1")
    monkeypatch.setattr(bootstrap, "CATALOG_FILE", broken)

    bootstrap.prepare(backend)  # не должно бросить


# ---------- файлы размещения ----------

def test_dockerfile_uses_the_hosting_port():
    """CMD должен разворачивать $PORT.

    Exec-форма CMD переменные не подставляет — это классическая ошибка,
    после которой сервис слушает строку "${PORT}" и не поднимается.
    """
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "${PORT" in text
    assert "--host 0.0.0.0" in text
    assert "--proxy-headers" in text


def test_dockerfile_does_not_run_as_root():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER\s+app", text, re.MULTILINE)


def test_render_blueprint_asks_for_the_database_url():
    """Строки подключения в репозитории быть не должно.

    `sync: false` означает «спросить у человека». Пароль, попавший
    в git, считается скомпрометированным навсегда: история остаётся
    даже после правки файла.
    """
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "DATABASE_URL" in text
    assert "sync: false" in text
    assert "healthCheckPath: /healthz" in text
    assert "postgres://" not in text and "postgresql://" not in text


def test_requirements_are_bounded():
    """У каждой зависимости есть верхняя граница.

    Без неё мажорное обновление приезжает в сборку само и ломает
    деплой в день, когда код никто не трогал.
    """
    raw = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lines = [
        line.strip() for line in raw.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    assert all("<" in line for line in lines), lines


def test_secrets_are_not_committed():
    """.env не должен попасть в репозиторий."""
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignored


# ---------- ограничение частоты по HTTP ----------

fastapi = pytest.importorskip("fastapi", reason="FastAPI не установлен")
from fastapi.testclient import TestClient  # noqa: E402

from shopapi import api  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SHOP_SQLITE_PATH", str(tmp_path / "limit.sqlite3"))
    with TestClient(api.app) as made:
        yield made


def _register(client, n: int):
    return client.post("/api/auth/register", json={
        "name": f"Гость {n}", "email": f"guest{n}@example.com",
        "password": "parol-na-vosem",
    })


def test_registration_is_rate_limited(client):
    """Открытая форма регистрации — первое, что находят боты.

    Без ограничения бесплатную базу на полгигабайта забивают мусорными
    профилями за вечер, и сайт ложится не от нагрузки, а от переполнения.
    """
    limit = api.LIMITS["register"].limit
    for n in range(limit):
        assert _register(client, n).status_code == 201

    blocked = _register(client, limit)
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_requests"


def test_429_tells_when_to_come_back(client):
    """Retry-After — стандартный заголовок, его понимают сами клиенты.

    Без него вежливому клиенту остаётся гадать, и он либо ждёт лишнее,
    либо долбится снова.
    """
    limit = api.LIMITS["register"].limit
    for n in range(limit + 1):
        response = _register(client, n)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_login_lockout_blocks_password_guessing(client):
    """Подбор пароля упирается в паузу, а не идёт бесконечно."""
    _register(client, 0)
    rule = api.LIMITS["login"]

    codes = [
        client.post("/api/auth/login", json={
            "email": "guest0@example.com", "password": "nepravilnyy-parol",
        }).status_code
        for _ in range(rule.limit + 1)
    ]

    assert codes[:rule.limit] == [401] * rule.limit
    assert codes[-1] == 429


def test_browsing_the_catalog_is_not_limited(client):
    """Ограничение стоит только на изменяющих ручках.

    Лимит на чтение каталога сломал бы обычное хождение по сайту:
    одна страница — это уже несколько запросов.
    """
    codes = {client.get("/api/products").status_code for _ in range(60)}
    assert codes == {200}


def test_limit_can_be_switched_off(client, monkeypatch):
    """Выключатель нужен для локальной отладки и нагрузочных прогонов."""
    monkeypatch.setenv("SHOP_RATE_LIMIT", "0")
    limit = api.LIMITS["register"].limit
    codes = [_register(client, n).status_code for n in range(limit + 3)]
    assert codes == [201] * (limit + 3)


# ---------- честность публичного демо ----------

def test_every_page_says_it_is_a_demo(client):
    """Полоса «это демо» должна быть в самой оболочке, а не на одной
    странице.

    Сайт открыт всем и выглядит как настоящий магазин. Человек, попавший
    сюда по ссылке, должен понимать, что товары не продаются, а отзывы
    и профили — тестовые данные, ещё до того, как что-то нажмёт.
    Раньше это было написано только в разделах «Доставка» и «Контакты» —
    то есть там, куда с главной никто не заходит.
    """
    for path in ("/", "/reviews", "/delivery", "/contacts", "/about"):
        html = client.get(path).text
        assert 'class="demobar"' in html, path
        assert "Демонстрационный проект" in html, path
