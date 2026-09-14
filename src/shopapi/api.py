"""HTTP-слой: JSON API и витрина.

Задача слоя ровно одна — перевести HTTP в вызовы сервисов и обратно.
Бизнес-логики здесь нет, она в `services/`, и её можно вызвать из CLI
или из фоновой задачи без всякого HTTP.

Что показано:
  * pydantic-схемы отдельно от доменных моделей;
  * единый обработчик доменных ошибок вместо try/except в каждой ручке;
  * идемпотентность через заголовок, как в платёжных API;
  * сквозной request_id в логах и в ответе;
  * витрина отдаётся тем же приложением — без отдельного фронтенд-сервера
    и без CORS, потому что источник один.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .errors import (
    DuplicateReview,
    Forbidden,
    NotAuthenticated,
    NotFound,
    ShopError,
    TooManyRequests,
)
from .factory import make_backend
from .ratelimit import Limiter, Rule, client_ip
from .services.orders import OrderRequest

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
STATE: dict = {}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on", "да"}


# ---------- ограничение частоты ----------
#
# Лимиты подобраны так, чтобы живой человек их не заметил, а перебор
# упёрся. Самый жёсткий — на вход: пять неудачных попыток подряд стоят
# пятиминутной паузы, и подбор пароля перестаёт быть вопросом времени.
#
# Считается по адресу клиента. Это не идеальный ключ (за одним адресом
# может сидеть целый офис), но единственный доступный до того, как
# человек представился, — а ограничивать надо именно такие запросы.

limiter = Limiter()

LIMITS = {
    "register": Rule(limit=5, window_seconds=3600),
    "login": Rule(limit=10, window_seconds=300, block_seconds=300),
    "review": Rule(limit=10, window_seconds=3600),
    "order": Rule(limit=30, window_seconds=600),
}

# X-Forwarded-For читается, только если сервис ЗАВЕДОМО за прокси:
# на хостинге это так, на localhost — нет.
TRUST_PROXY = _flag("SHOP_TRUST_PROXY")

# Сколько СВОИХ прокси стоит перед сервисом. Число важное: адрес клиента
# берётся справа по этому счётчику, потому что прокси дописывают себя
# в конец, а присланное клиентом остаётся слева. Подробности и разбор
# ошибки «взять первый элемент» — в ratelimit.py.
try:
    PROXY_HOPS = max(int(os.environ.get("SHOP_PROXY_HOPS", "1")), 1)
except ValueError:
    PROXY_HOPS = 1

# Заголовок периметра, который тот перезаписывает, а не дописывает
# (у Cloudflare это CF-Connecting-IP). Если задан и пришёл — он
# надёжнее разбора цепочки.
CLIENT_IP_HEADER = os.environ.get("SHOP_CLIENT_IP_HEADER", "").strip() or None


def rate_limit(action: str):
    """Зависимость-ограничитель для конкретного действия."""
    rule = LIMITS[action]

    def dependency(request: Request) -> None:
        if not _flag("SHOP_RATE_LIMIT", default=True):
            return
        who = client_ip(request, TRUST_PROXY, hops=PROXY_HOPS, trusted_header=CLIENT_IP_HEADER)
        limiter.hit(f"{who}:{action}", rule)

    return dependency


# ---------- схемы ----------

class OrderItemIn(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=100)


class OrderIn(BaseModel):
    """Ограничения заданы в типах: FastAPI вернёт 422 с понятным
    описанием ещё до того, как запрос дойдёт до сервиса.

    `customer_id` из ТЕЛА запроса больше не читается: покупатель
    берётся из сессии. Иначе любой мог бы оформить заказ от чужого
    имени, просто подставив число — и получить право на отзыв
    о чужих покупках.
    """

    items: list[OrderItemIn] = Field(min_length=1, max_length=50)


class ReviewIn(BaseModel):
    """Отзыв с витрины.

    Длины заданы здесь, а не только в базе: отклонить мегабайт текста
    надо до того, как он доедет до диска. В схеме те же ограничения
    продублированы CHECK-ами — база остаётся последним рубежом,
    потому что клиент может быть и не наш.
    """

    author: str = Field(min_length=1, max_length=80)
    rating: int = Field(ge=1, le=5)
    body: str = Field(min_length=1, max_length=4000)
    title: str = Field(default="", max_length=120)
    product_id: int | None = Field(default=None, gt=0)


# ---------- жизненный цикл ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Бэкенд собирается ОДИН раз на старте.

    Открытие соединения к Postgres — это рукопожатие, аутентификация
    и запуск процесса на сервере, десятки миллисекунд. Делать это
    на каждый запрос нельзя, поэтому пул создаётся здесь.
    """
    from . import bootstrap

    backend = make_backend(dsn=STATE.get("dsn"), sqlite_path=STATE.get("sqlite_path"))
    STATE["backend"] = backend
    bootstrap.prepare(backend)
    logger.info("Сервис готов, база: %s", backend.kind)

    task = asyncio.create_task(housekeeping(backend)) if _flag("SHOP_DEMO") else None

    yield

    if task is not None:
        task.cancel()
        # Отменённую задачу надо дождаться, а не просто пометить:
        # без await отмена может не успеть отработать до закрытия
        # пула, и в лог прилетит ошибка уже на выходе.
        with suppress(asyncio.CancelledError):
            await task
    backend.close()
    logger.info("Сервис остановлен")


HOUSEKEEPING_INTERVAL = 1800  # полчаса


async def housekeeping(backend) -> None:
    """Фоновое обслуживание публичного демо.

    Делает две вещи: возвращает распроданные остатки и чистит словарь
    ограничителя от давно замолчавших адресов.

    Работа с базой уходит в отдельный поток: репозитории синхронные,
    и вызов их прямо в корутине заблокировал бы весь цикл событий —
    на время обновления остатков сервис перестал бы отвечать всем.
    """
    while True:
        try:
            await asyncio.sleep(HOUSEKEEPING_INTERVAL)
            from . import bootstrap

            await asyncio.to_thread(bootstrap.restock, backend)
            limiter.purge()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Фоновая задача не имеет права утащить за собой сервис:
            # упавшая уборка — это повод для строчки в логе, а не для
            # пятисотки посетителю.
            logger.exception("Фоновое обслуживание не отработало")


app = FastAPI(
    title="GAMEPORT Shop API",
    version="6.0.0",
    description="Магазин игр и консолей: каталог, заказы, остатки.",
    lifespan=lifespan,
)


# ---------- сквозная обработка ----------

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Сквозной идентификатор запроса.

    Без него в логах нагруженного сервиса строки одного запроса
    перемешаны с сотней чужих. С ним разбор инцидента — это grep
    по одному значению, а клиент может прислать id из заголовка ответа.
    """
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
    # Статика отдаётся БЕЗ кэша: обновил страницу — получил свежие
    # css и js, без Ctrl+Shift+R и без «почему у меня всё разъехалось».
    #
    # Для рабочего сервиса это неправильно: файлы качались бы заново
    # на каждый заход, и сайт стал бы медленнее. Там поступают наоборот —
    # `max-age=31536000, immutable` плюс версия в адресе: браузер
    # не перепроверяет файл вообще, а обновление приходит вместе
    # с новым адресом.
    #
    # Здесь выбран удобный для разработки вариант осознанно, и версия
    # в адресе оставлена: она страхует на случай, если между браузером
    # и сервисом окажется прокси со своим кэшем, которому наш заголовок
    # не указ.
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"

    if not request.url.path.startswith("/static"):
        logger.info(
            "%s %s -> %s за %.1f мс [%s]",
            request.method, request.url.path, response.status_code, elapsed_ms, request_id,
        )
    return response


@app.exception_handler(ShopError)
async def handle_shop_error(request: Request, exc: ShopError) -> JSONResponse:
    """Один обработчик на все доменные ошибки.

    Код ответа берётся из самого исключения. Добавить новый тип ошибки —
    значит просто объявить класс; try/except в каждой ручке не нужен,
    и никто не забудет его написать.
    """
    logger.warning("Доменная ошибка %s: %s", exc.code, exc.message)
    headers = {}
    if isinstance(exc, TooManyRequests):
        # Стандартный заголовок вместо «написано в тексте ошибки»:
        # его понимают клиентские библиотеки и умеют ждать сами.
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.http_status, content=exc.as_dict(), headers=headers)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    """Ошибки валидации — в том же формате, что и доменные.

    По умолчанию FastAPI отдаёт свой `{"detail": [...]}`, и клиенту
    приходится разбирать два разных формата ошибок. Единый формат
    означает, что на фронтенде один обработчик, а не два.
    """
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", [])[1:]) or "запрос"
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": f"Некорректный запрос: {field} — {first.get('msg', 'ошибка')}",
            "details": {"errors": jsonable_encoder(exc.errors())},
        },
    )


def backend():
    return STATE["backend"]


def products_repo():
    return backend().products


def orders_repo():
    return backend().orders


def reviews_repo():
    return backend().reviews


def customers_repo():
    return backend().customers


def order_service():
    return backend().order_service


def session_token(authorization: str | None = Header(default=None)) -> str | None:
    """Токен из заголовка `Authorization: Bearer ...`.

    Стандартный заголовок, а не свой собственный: его понимают прокси,
    клиентские библиотеки и инструменты, и он не попадает в логи
    так же охотно, как параметр в адресе. Токен в query-строке —
    распространённая ошибка: адреса пишутся в журналы доступа
    целиком, и сессия утекает туда вместе с ними.
    """
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def current_customer_row(
    token: str | None = Depends(session_token),
    customers=Depends(customers_repo),
) -> dict | None:
    """Покупатель по живой сессии — или None.

    РАНЬШЕ идентификатор приходил прямо заголовком `X-Customer-Id`,
    и это означало, что представиться кем угодно можно было одной
    строкой в консоли браузера. Теперь личность подтверждается токеном,
    который выдаётся только после проверки пароля, а срок сессии
    проверяется в самом запросе к базе.

    Возвращается вся строка, а не только id: почти всем ручкам, которым
    нужен покупатель, нужно и его имя, и повторный запрос за ним был бы
    лишним походом в базу на каждый вызов.
    """
    if not token:
        return None
    return customers.customer_by_token(token)


def current_customer(row: dict | None = Depends(current_customer_row)) -> int | None:
    """Только идентификатор — для ручек, которым больше ничего не нужно."""
    return row["id"] if row else None


def require_customer(row: dict | None = Depends(current_customer_row)) -> dict:
    """То же, но обязательно. Без сессии — 401.

    Отдельная зависимость, а не проверка в каждой ручке: забыть её
    в одном месте легко, и тогда ручка молча начнёт работать
    для анонимов.
    """
    if row is None:
        raise NotAuthenticated("нужно войти в профиль")
    return row


# ---------- витрина ----------
#
# КЭШИРОВАНИЕ СТАТИКИ.
#
# Здесь была ошибка, которую видит только пользователь. Витрина
# ссылалась на `/static/app.css` без версии, браузер честно клал файл
# в кэш — и после обновления проекта показывал НОВЫЙ html со СТАРЫМ
# css. Страница разъезжалась: разметка из свежей версии, стили
# из прошлой. У разработчика при этом всё в порядке: он жмёт
# Ctrl+Shift+R по привычке.
#
# Лечится связкой из двух правил:
#
#   1. адрес файла содержит версию — `/static/app.css?v=4.1.0`.
#      Поменялась версия, поменялся адрес, кэш прошлого файла
#      к нему не относится;
#
#   2. сам html НЕ кэшируется (`no-cache`). Иначе браузер оставил бы
#      у себя старую страницу со старой версией в ссылках, и пункт 1
#      не сработал бы никогда.
#
# То есть кэшируется то, у чего адрес меняется вместе с содержимым,
# и не кэшируется то, что этот адрес выдаёт.

ASSET_VERSION = app.version


@lru_cache(maxsize=1)
def index_html() -> str:
    """Оболочка витрины с подставленной версией.

    Читается один раз: файл не меняется в работающем процессе,
    а читать его с диска на каждый заход — лишний ввод-вывод
    на самом частом запросе сервиса.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("__ASSET_VERSION__", ASSET_VERSION)


def storefront_response() -> HTMLResponse:
    return HTMLResponse(
        index_html(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/", include_in_schema=False)
async def storefront() -> HTMLResponse:
    return storefront_response()


# ---------- API каталога ----------

@app.get("/api/products")
async def list_products(
    category: str | None = None,
    search: str | None = None,
    platform: str | None = None,
    repo=Depends(products_repo),
) -> dict:
    """Каталог для витрины: товары, категории и сводка одним ответом.

    Собрано в один запрос намеренно: витрине при старте нужно и то,
    и другое, а два похода по сети вместо одного — это лишняя задержка
    и мигающий интерфейс.
    """
    items = repo.list_all(category=category, search=search, platform=platform)

    # Рейтинги берутся ОДНИМ запросом на весь список, а не по одному
    # на карточку. Иначе каталог из сорока трёх товаров превращается
    # в сорок четыре запроса — классический N+1, который на витрине
    # заметен сразу, а в коде выглядит невинным вызовом в цикле.
    ratings = backend().reviews.ratings_by_product([row["id"] for row in items])

    return {
        "items": [
            {
                "id": row["id"],
                "sku": row["sku"],
                "title": row["title"],
                "description": row.get("description", ""),
                "category": row.get("category", "Прочее"),
                "emoji": row.get("emoji", "📦"),
                "platform": row.get("platform", ""),
                "genre": row.get("genre", ""),
                "developer": row.get("developer", ""),
                "year": row.get("year", 0),
                "art": f"/static/art/{row['sku']}.svg",
                "price_kopecks": row["price_kopecks"],
                "stock": row["stock"],
                "rating": ratings.get(row["id"], {}).get("average", 0),
                "reviews_count": ratings.get(row["id"], {}).get("total", 0),
            }
            for row in items
        ],
        "categories": [
            {"category": c["category"], "count": int(c["count"])}
            for c in repo.categories()
        ],
        "platforms": [
            {"platform": pl["platform"], "count": int(pl["count"])}
            for pl in repo.platforms()
        ],
        "total_stock": sum(row["stock"] for row in items),
        "backend": backend().kind,
    }


@app.get("/api/products/{product_id}")
async def get_product(product_id: int, repo=Depends(products_repo)) -> dict:
    return repo.get_full(product_id)


# ---------- API заказов ----------

@app.post("/api/orders", status_code=201, dependencies=[Depends(rate_limit("order"))])
async def create_order(
    payload: OrderIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service=Depends(order_service),
    profile: dict = Depends(require_customer),
) -> dict:
    """Оформление заказа.

    Ключ идемпотентности приходит ЗАГОЛОВКОМ, а не в теле — так устроены
    платёжные API, и по той же причине: ключ относится к доставке
    запроса, а не к его смыслу. Повтор с тем же ключом вернёт тот же
    заказ, а не создаст второй.

    Покупатель берётся ИЗ СЕССИИ, а не из тела запроса. Это разница
    между «клиент сообщает, кто он» и «сервер знает, кто он»: первое
    подделывается одной строкой.
    """
    order = service.create_order(
        OrderRequest(
            customer_id=profile["id"],
            items=[(item.product_id, item.quantity) for item in payload.items],
            idempotency_key=idempotency_key,
        )
    )
    return {
        "id": order.id,
        "status": order.status.value,
        "total_kopecks": order.total_kopecks,
        "lines": [
            {
                "product_id": line.product_id,
                "sku": line.sku,
                "quantity": line.quantity,
                "price_kopecks": line.price_kopecks,
            }
            for line in order.lines
        ],
    }


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(
    order_id: int,
    service=Depends(order_service),
    orders=Depends(orders_repo),
    profile: dict = Depends(require_customer),
) -> dict:
    """Отменить можно ТОЛЬКО свой заказ.

    Без этой проверки отмена чужого заказа — это перебор чисел
    в адресе. Ошибка называется «небезопасная прямая ссылка
    на объект», и она стабильно попадает в списки самых частых:
    проверку прав забывают там, где идентификатор выглядит
    безобидным числом.

    Чужой заказ отдаёт 404, а не 403: иначе по коду ответа можно
    перебором узнать, какие номера заказов существуют.
    """
    existing = orders.get(order_id)
    if existing.customer_id != profile["id"]:
        raise NotFound("заказ не найден", order_id=order_id)

    order = service.cancel_order(order_id)
    return {"id": order.id, "status": order.status.value}


def _format_time(value) -> str:
    """Время для витрины в одном виде независимо от базы.

    PostgreSQL отдаёт `datetime` (тип TIMESTAMPTZ), sqlite — строку ISO:
    отдельного типа даты у него нет. Разница не должна доходить
    до интерфейса, иначе один и тот же экран выглядит по-разному
    на двух базах.
    """
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y %H:%M")
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(value)


@app.get("/api/orders/recent")
async def recent_orders(repo=Depends(orders_repo)) -> dict:
    """Последние заказы для витрины.

    Раньше здесь стояла проверка `hasattr(repo, "recent")` с пустым
    ответом: страховка на случай базы, где метода нет. Страховка
    сработала ровно наоборот — на sqlite витрина молча показывала
    «заказов нет» вместо ошибки, и расхождение репозиториев жило
    незамеченным. Оба репозитория обязаны уметь `recent`, и это
    проверяется тестом, а не условием в рантайме.
    """
    rows = repo.recent(limit=8)
    total = repo.db.query_one("SELECT COUNT(*) AS n FROM orders")
    return {
        "items": [
            {
                "id": row["id"],
                "status": row["status"],
                "items": int(row["items"]),
                "total_kopecks": int(row["total_kopecks"]),
                "created_at": _format_time(row["created_at"]),
            }
            for row in rows
        ],
        "total": int(total["n"]) if total else 0,
    }


# ---------- API отзывов ----------

@app.get("/api/reviews")
async def list_reviews(
    product_id: int | None = None,
    limit: int = 50,
    repo=Depends(reviews_repo),
) -> dict:
    """Отзывы и сводка по оценкам одним ответом.

    Как и каталог, собрано вместе: странице отзывов нужны и список,
    и распределение по звёздам сразу, а два запроса вместо одного —
    это лишний круг по сети и подпрыгивающая вёрстка.
    """
    limit = max(1, min(limit, 200))
    rows = repo.list_recent(limit=limit, product_id=product_id)
    summary = repo.summary()
    return {
        "items": [
            {
                "id": row["id"],
                "author": row["author"],
                "rating": int(row["rating"]),
                "title": row.get("title", ""),
                "body": row["body"],
                "created_at": _format_time(row["created_at"]),
                "product_title": row.get("product_title"),
                "product_id": row.get("product_id"),
                # Пометка уходит НА ВИТРИНУ, а не остаётся в базе:
                # выдуманный отзыв не должен выглядеть как настоящий.
                "is_demo": bool(row.get("is_demo")),
            }
            for row in rows
        ],
        "summary": {
            "total": int(summary.get("total", 0) or 0),
            "average": round(float(summary.get("average", 0) or 0), 2),
            "stars": {
                str(n): int(summary.get(f"stars{n}", 0) or 0)
                for n in range(1, 6)
            },
        },
    }


@app.post("/api/reviews", status_code=201, dependencies=[Depends(rate_limit("review"))])
async def create_review(
    payload: ReviewIn,
    repo=Depends(reviews_repo),
    customers=Depends(customers_repo),
    customer_id: int | None = Depends(current_customer),
) -> dict:
    """Отзыв о товаре — только после покупки.

    Проверка стоит ЗДЕСЬ, а не в интерфейсе. Кнопку на витрине легко
    спрятать, но POST на этот адрес может отправить кто угодно из
    консоли браузера. Правило, которое держится только на вёрстке,
    правилом не является.

    Три ответа вместо одного «нельзя»:

      401 — непонятно, кто пришёл: покупатель не выбран;
      403 — покупатель известен, но этого товара у него нет;
      409 — отзыв на этот товар он уже оставлял.

    Разные коды нужны клиенту: на 401 предлагают войти, на 403
    показывают отказ, на 409 — существующий отзыв.

    Отзыв о магазине целиком (без товара) оставить может любой:
    для него покупка не требуется.
    """
    verified = False

    if payload.product_id is not None:
        if not customer_id:
            raise NotAuthenticated(
                "чтобы оставить отзыв о товаре, выберите покупателя",
                product_id=payload.product_id,
            )
        if not customers.has_purchased(customer_id, payload.product_id):
            raise Forbidden(
                "отзыв можно оставить только на купленный товар",
                product_id=payload.product_id,
            )
        verified = True

    try:
        review_id = repo.add(
            author=payload.author,
            rating=payload.rating,
            body=payload.body,
            title=payload.title,
            product_id=payload.product_id,
            is_demo=False,
            customer_id=customer_id,
            verified=verified,
        )
    except Exception as exc:  # noqa: BLE001
        # «Один отзыв на товар» — уникальный индекс в базе, а не проверка
        # в коде: проверка «сначала SELECT, потом INSERT» это гонка,
        # два одновременных запроса оба увидят, что отзыва нет.
        # Ловим нарушение и переводим в понятную доменную ошибку.
        if _is_unique_violation(exc):
            raise DuplicateReview(
                "вы уже оставляли отзыв на этот товар",
                product_id=payload.product_id,
            ) from exc
        raise

    return {"id": review_id, "status": "ok", "verified": verified}


def _is_unique_violation(exc: Exception) -> bool:
    """Нарушение уникальности — по-разному в двух драйверах.

    sqlite3 бросает IntegrityError с текстом «UNIQUE constraint failed»,
    psycopg — UniqueViolation. Проверять по тексту неприятно, но
    альтернатива — тащить импорт psycopg в слой, который должен
    работать и без него.
    """
    name = type(exc).__name__
    if name in {"UniqueViolation", "IntegrityError"}:
        return "unique" in str(exc).lower() or name == "UniqueViolation"
    return False


# ---------- API страницы товара ----------

@app.get("/api/products/by-sku/{sku}")
async def get_product_page(
    sku: str,
    repo=Depends(products_repo),
    reviews=Depends(reviews_repo),
    customers=Depends(customers_repo),
    customer_id: int | None = Depends(current_customer),
) -> dict:
    """Всё для страницы товара — ОДНИМ ответом.

    Товар, характеристики, отзывы, сводка по оценкам, рекомендации
    и право оставить отзыв. Шесть отдельных запросов с витрины дали бы
    шесть кругов по сети и страницу, которая доезжает кусками
    и подпрыгивает по мере загрузки.

    Обратная сторона такого решения — ответ становится тяжелее, и часть
    данных может быть не нужна. Здесь это оправдано: страница товара
    показывает всё сразу, ничего не подгружая по клику.
    """
    product = repo.get_by_sku(sku)
    product_id = product["id"]

    return {
        "product": _product_payload(product),
        "specs": _parse_specs(product.get("specs")),
        "reviews": [_review_payload(row) for row in
                    reviews.list_recent(limit=50, product_id=product_id)],
        "rating": _rating_payload(reviews.summary(product_id=product_id)),
        "recommendations": [
            _product_payload(row) for row in repo.recommendations(product_id, limit=4)
        ],
        # Право на отзыв считает СЕРВЕР. Витрина может спрятать форму,
        # но решение принимает не она: запрос можно послать и напрямую.
        "can_review": bool(customer_id) and customers.has_purchased(customer_id, product_id),
        # Уже оставленный отзыв — отдельный признак, а не отсутствие
        # права. Разница в том, что показать: «купите, чтобы оценить»
        # или «вы уже оценили». Одним флагом эти два случая
        # не различить, и человек видел бы форму, которая гарантированно
        # вернёт 409.
        "already_reviewed": bool(customer_id)
        and product_id in reviews.reviewed_product_ids(customer_id),
    }


def _parse_specs(raw) -> dict:
    """Характеристики из колонки в словарь.

    Битый JSON не должен ронять страницу целиком: товар с испорченным
    полем покажется без таблицы характеристик, и это лучше, чем 500
    на весь раздел.
    """
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        logger.warning("не разобрал характеристики: %r", raw)
        return {}
    return value if isinstance(value, dict) else {}


def _product_payload(row) -> dict:
    return {
        "id": row["id"],
        "sku": row["sku"],
        "title": row["title"],
        "description": row.get("description", ""),
        "category": row.get("category", "Прочее"),
        "emoji": row.get("emoji", "📦"),
        "platform": row.get("platform", ""),
        "genre": row.get("genre", ""),
        "developer": row.get("developer", ""),
        "year": row.get("year", 0),
        "art": f"/static/art/{row['sku']}.svg",
        "price_kopecks": row["price_kopecks"],
        "stock": row["stock"],
    }


def _review_payload(row) -> dict:
    return {
        "id": row["id"],
        "author": row["author"],
        "rating": int(row["rating"]),
        "title": row.get("title", ""),
        "body": row["body"],
        "created_at": _format_time(row["created_at"]),
        "product_title": row.get("product_title"),
        "product_sku": row.get("product_sku"),
        "product_id": row.get("product_id"),
        "city": row.get("customer_city") or "",
        "verified": bool(row.get("verified")),
        "is_demo": bool(row.get("is_demo")),
    }


def _rating_payload(summary: dict) -> dict:
    return {
        "total": int(summary.get("total", 0) or 0),
        "average": round(float(summary.get("average", 0) or 0), 2),
        "stars": {str(n): int(summary.get(f"stars{n}", 0) or 0) for n in range(1, 6)},
    }


# ---------- регистрация и вход ----------

class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    city: str = Field(default="", max_length=80)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


def _session_payload(profile: dict, token: str) -> dict:
    """Ответ на регистрацию и вход.

    Пароль и его хеш сюда не попадают — ни в каком виде. Это звучит
    очевидно, но `dict(row)` из базы содержит `password_hash`, и отдать
    строку целиком «чтобы не перечислять поля» — самый простой способ
    его опубликовать.
    """
    return {
        "token": token,
        "profile": {
            "id": profile["id"],
            "name": profile["name"],
            "email": profile["email"],
            "city": profile.get("city", ""),
            "joined_at": _format_time(profile["joined_at"]),
        },
    }


@app.post("/api/auth/register", status_code=201,
          dependencies=[Depends(rate_limit("register"))])
async def register(payload: RegisterIn, customers=Depends(customers_repo)) -> dict:
    """Создаёт профиль и сразу открывает сессию.

    Сразу — потому что заставлять человека вводить те же данные второй
    раз незачем: он только что доказал, что знает пароль.
    """
    customer_id = customers.register(
        name=payload.name, email=payload.email,
        password=payload.password, city=payload.city,
    )
    profile = customers.get(customer_id)
    token = customers.start_session(customer_id)
    logger.info("Зарегистрирован покупатель %s", customer_id)
    return _session_payload(profile, token)


@app.post("/api/auth/login", dependencies=[Depends(rate_limit("login"))])
async def login(payload: LoginIn, customers=Depends(customers_repo)) -> dict:
    profile = customers.authenticate(payload.email, payload.password)
    token = customers.start_session(profile["id"])
    return _session_payload(profile, token)


@app.post("/api/auth/logout", status_code=204)
async def logout(
    token: str | None = Depends(session_token),
    customers=Depends(customers_repo),
):
    """Выход гасит сессию В БАЗЕ, а не только в браузере.

    Удалить токен у себя мало: копия могла остаться где угодно,
    и она продолжала бы работать до истечения срока.
    """
    if token:
        customers.end_session(token)
    return Response(status_code=204)


@app.get("/api/auth/me")
async def whoami(row: dict | None = Depends(current_customer_row)) -> dict:
    """Кто вошёл. Без сессии — пустой ответ, а не ошибка.

    Витрина спрашивает это при загрузке, чтобы решить, показывать
    «Войти» или имя. Анонимный посетитель — обычное состояние,
    а не сбой, и отвечать ему 401 значило бы писать ошибку в консоль
    на каждом заходе.
    """
    if row is None:
        return {"profile": None}
    return {
        "profile": {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "city": row.get("city", ""),
            "joined_at": _format_time(row["joined_at"]),
        }
    }


# ---------- API покупателей ----------

# Ручки со списком ВСЕХ покупателей здесь больше нет — и это
# осознанное удаление, а не упрощение. Она отдавала имена, города
# и адреса почты всех зарегистрированных людей любому, кто откроет
# сайт. Для демонстрации было удобно; как поведение сервиса —
# неприемлемо: это персональные данные, и показывать их посторонним
# нельзя ни при каких обстоятельствах.
#
# Витрине она была нужна ради выбора «кто я». Теперь личность
# подтверждается паролем, и выбирать не из чего.


@app.get("/api/customers/me")
async def get_me(
    customers=Depends(customers_repo),
    orders=Depends(orders_repo),
    reviews=Depends(reviews_repo),
    profile: dict = Depends(require_customer),
) -> dict:
    """Профиль, покупки и то, на что ещё можно оставить отзыв."""
    customer_id = profile["id"]
    purchased = customers.purchased_products(customer_id)

    # Отзывы ИМЕННО этого покупателя, отдельным запросом. Брать общий
    # список «последних N» и фильтровать его нельзя: ограничение там
    # сделано для показа, а не для логики, и на шестистах отзывах
    # старые записи покупателя в окно уже не попадают.
    written = reviews.reviewed_product_ids(customer_id)

    return {
        "profile": {
            "id": profile["id"],
            "name": profile["name"],
            "email": profile["email"],
            "city": profile.get("city", ""),
            "joined_at": _format_time(profile["joined_at"]),
        },
        # list_for_customer отдаёт ДОМЕННЫЕ объекты Order, а не строки
        # базы: это ровно то, ради чего есть слой моделей — сервисы
        # работают с типами, а не с кортежами из курсора. Обращение
        # здесь идёт через атрибуты, а не по ключу.
        "orders": [
            {
                "id": order.id,
                "status": order.status.value,
                "created_at": _format_time(order.created_at),
                "lines": [
                    {"sku": line.sku, "quantity": line.quantity,
                     "price_kopecks": line.price_kopecks}
                    for line in order.lines
                ],
                "total_kopecks": order.total_kopecks,
            }
            for order in orders.list_for_customer(customer_id, limit=20)
        ],
        "purchased": [
            {
                "product_id": row["product_id"],
                "sku": row["sku"],
                "title": row["title"],
                "platform": row.get("platform", ""),
                "art": f"/static/art/{row['sku']}.svg",
                "last_order_id": row["last_order_id"],
                "reviewed": row["product_id"] in written,
            }
            for row in purchased
        ],
    }


# ---------- эксплуатация ----------

@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Процесс жив."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz():
    """Готов принимать трафик.

    Отличается от `healthz`: во время прогрева процесс жив, но работать
    не может, и балансировщик не должен слать на него запросы. Здесь
    проверяется настоящий запрос к базе, а не наличие объекта в памяти —
    иначе проба зелёная, а база недоступна.
    """
    if "backend" not in STATE:
        return JSONResponse(status_code=503, content={"status": "starting"})
    try:
        backend().db.query_one("SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001
        logger.error("readyz: база недоступна: %s", exc)
        return JSONResponse(status_code=503, content={"status": "db_unavailable"})
    return {"status": "ready", "backend": backend().kind}


# ---------- страницы витрины ----------
#
# ЭТА РУЧКА ОБЪЯВЛЕНА ПОСЛЕДНЕЙ, И ЭТО ОБЯЗАТЕЛЬНО.
#
# FastAPI подбирает маршрут перебором в порядке объявления, а `/{page}`
# подходит под любой односегментный путь. Пока она стояла выше, она
# перехватывала `/healthz` и `/readyz`: пробы получали 404, и в проде
# это означало бы, что балансировщик считает живой сервис мёртвым
# и снимает его с трафика. Поймано тестом
# `test_api_paths_are_not_swallowed_by_page_route`, а не глазами —
# приложение при этом поднималось и витрина работала.
#
# Общее правило: маршрут с параметром в корне пути опасен, и место ему
# в самом конце. Именно поэтому ниже него стоит только монтирование
# статики — по той же причине.

PAGES = {"catalog", "reviews", "delivery", "contacts", "about", "profile"}


@app.get("/product/{sku}", include_in_schema=False)
async def storefront_product(sku: str):
    """Страница товара по прямому адресу.

    Объявлена ДО `/{page}`: иначе односегментная ручка не совпала бы
    вовсе (у пути два сегмента), но порядок здесь всё равно важен —
    он держит правило «сначала конкретное, потом общее» на виду.

    Существование артикула тут не проверяется: страница одна и та же,
    а «товар не найден» витрина покажет сама, получив 404 от API.
    Ходить в базу ради отдачи HTML — лишний запрос на каждый заход.
    """
    return storefront_response()


@app.get("/{page}", include_in_schema=False)
async def storefront_page(page: str):
    """Отдаёт ту же страницу на любой адрес раздела.

    Зачем это нужно. Витрина переключает разделы через History API:
    адрес в строке меняется на /reviews, перезагрузки нет. Но если
    человек НАЖМЁТ F5 на этом адресе или откроет присланную ссылку,
    браузер честно спросит `/reviews` у сервера. Без этого обработчика
    ответом будет 404 — классическая поломка одностраничных приложений,
    которая не воспроизводится при обычном хождении по сайту и потому
    всплывает уже у пользователей.

    Дальше разбирается сам роутер в браузере: он читает адрес
    и показывает нужный раздел.

    Список разделов задан явно, а не «отдаём index.html на всё
    подряд»: на несуществующий адрес должен приходить 404, иначе
    опечатка в ссылке молча покажет главную, и поисковики
    проиндексируют десяток дублей одной страницы.
    """
    if page not in PAGES:
        return JSONResponse(
            status_code=404,
            content={"code": "not_found", "message": f"нет такой страницы: /{page}"},
        )
    return storefront_response()


# Статика монтируется ПОСЛЕ ручек: иначе она перехватила бы пути,
# начинающиеся с того же префикса.
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
