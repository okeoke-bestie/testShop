"""Подготовка боевого экземпляра: наполнение базы и поддержание демо.

На своей машине каталог заливают руками — `python main.py seed`. На
хостинге этого сделать некому: контейнер поднимается сам, и если база
пустая, посетитель увидит витрину без товаров. Поэтому наполнение
вынесено в функцию, которую вызывает сам сервис при старте.

Ключевое правило здесь — **не трогать чужие данные**. Наполнение
происходит ровно один раз, пока каталог пуст. Иначе каждый передеплой
(а на бесплатном тарифе он случается и сам по себе, при пробуждении
сервиса) стирал бы заказы и отзывы посетителей.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CATALOG_FILE = DATA_DIR / "catalog.json"
REVIEWS_FILE = DATA_DIR / "reviews.json"
CUSTOMERS_FILE = DATA_DIR / "customers.json"


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def catalog_is_empty(backend) -> bool:
    row = backend.db.query_one("SELECT COUNT(*) AS n FROM products")
    return not (row and row["n"])


def seed_if_empty(backend) -> int:
    """Заливает каталог, демо-профили и демо-отзывы, если товаров нет.

    Возвращает число загруженных товаров (0 — значит, база уже была
    наполнена и её не трогали).
    """
    if not catalog_is_empty(backend):
        return 0
    if not CATALOG_FILE.exists():
        logger.warning("Каталог пуст, но файла %s нет — заливать нечего", CATALOG_FILE)
        return 0

    from .catalog_file import load_catalog, seed_database
    from .reviews_file import load_customers, load_reviews, seed_customers, seed_reviews

    products = seed_database(backend.products, load_catalog(CATALOG_FILE))
    logger.info("Каталог загружен: %s товаров", products)

    if CUSTOMERS_FILE.exists():
        people = seed_customers(backend.customers, load_customers(CUSTOMERS_FILE))
        logger.info("Демо-профили загружены: %s", people)
    if REVIEWS_FILE.exists():
        count = seed_reviews(
            backend.reviews, backend.products, load_reviews(REVIEWS_FILE),
            customers_repo=backend.customers,
        )
        logger.info("Демо-отзывы загружены: %s", count)

    return products


def restock(backend) -> int:
    """Возвращает остатки к значениям из файла каталога.

    Нужно только публичному демо. Товара по 15–30 штук, посетителей
    может быть много, и через сутки витрина превратится в список
    «нет в наличии» — сайт будет выглядеть сломанным, хотя работает
    правильно.

    Поднимаются ТОЛЬКО просевшие позиции (`stock < baseline`). Если
    остаток почему-то больше — значит, его меняли осознанно, и
    затирать это не надо. Заказы при этом не трогаются: история
    покупок у посетителя остаётся, и его право на отзыв тоже.
    """
    if not CATALOG_FILE.exists():
        return 0

    from .catalog_file import load_catalog

    items = load_catalog(CATALOG_FILE)
    db = backend.db
    ph = "%s" if backend.kind == "postgres" else "?"
    sql = f"UPDATE products SET stock = {ph} WHERE sku = {ph} AND stock < {ph}"

    restored = 0
    with db.transaction():
        for item in items:
            cursor = db.execute(sql, (item.stock, item.sku, item.stock))
            restored += max(getattr(cursor, "rowcount", 0) or 0, 0)
    if restored:
        logger.info("Остатки восстановлены у %s товаров", restored)
    return restored


def prepare(backend) -> None:
    """Всё, что нужно сделать один раз при старте сервиса.

    Наполнение включается переменной SHOP_AUTO_SEED. По умолчанию оно
    выключено: на своей машине базу наполняет человек, и сервис,
    молча пишущий в неё при каждом запуске, — неприятный сюрприз.
    В контейнере переменная выставлена в Dockerfile.
    """
    if not _flag("SHOP_AUTO_SEED"):
        return
    try:
        seed_if_empty(backend)
    except Exception:  # noqa: BLE001
        # Упасть здесь — значит не подняться вообще. Витрина без
        # каталога хуже, чем витрина с каталогом, но лучше, чем
        # страница хостинга «сервис не запустился»: остальное
        # приложение исправно, и причина видна в логах.
        logger.exception("Не удалось наполнить базу при старте")
