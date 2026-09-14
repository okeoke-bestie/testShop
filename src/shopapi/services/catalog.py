"""Сервис каталога: выдача товаров с кэшем.

Показывает две вещи: кэширование с инвалидацией и то, почему кэш нельзя
приделать «сверху» не подумав.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..models import Page, Product
from ..reliability import TTLCache

logger = logging.getLogger(__name__)


@dataclass
class CatalogService:
    products: object
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl=30.0))

    def get_product(self, product_id: int) -> Product:
        """Товар по id, с кэшем.

        Кэшируется КАРТОЧКА товара, но не остаток: остаток меняется
        каждым заказом, и закэшированное значение почти сразу
        становится ложью. Поэтому при оформлении заказа остаток всегда
        читается из базы, а кэш обслуживает только показ карточки.

        Это общее правило: кэшировать можно то, что меняется редко,
        а цена ошибки невелика. Данные, на основании которых
        принимаются решения о деньгах и остатках, читаются напрямую.
        """
        cached = self.cache.get(("product", product_id))
        if cached is not None:
            return cached
        product = self.products.get(product_id)
        self.cache.set(("product", product_id), product)
        return product

    def list_products(self, limit: int = 20, cursor: str | None = None) -> Page:
        """Страница каталога.

        Страницы намеренно НЕ кэшируются: ключом был бы курсор, а курсоров
        столько же, сколько позиций в каталоге. Кэш раздуется, а попаданий
        почти не будет — типичный случай, когда кэш только вредит.
        """
        return self.products.list_page(limit=limit, cursor=cursor)

    def invalidate(self, product_id: int) -> None:
        """Сброс кэша после изменения товара.

        Инвалидация — самая сложная часть кэширования. Здесь она простая
        только потому, что кэш локальный и живёт в одном процессе.
        В сервисе на нескольких экземплярах нужен общий кэш (Redis)
        или события об изменении: иначе один экземпляр сбросит кэш,
        а остальные продолжат отдавать старое.
        """
        self.cache.set(("product", product_id), None)
        self.cache._store.pop(("product", product_id), None)
        logger.debug("Кэш товара %s сброшен", product_id)

    def search(self, query: str, limit: int = 20) -> list[Product]:
        """Поиск по названию.

        Реализован на LIKE — и это осознанное упрощение, а не недосмотр.
        LIKE '%текст%' не может использовать обычный индекс: база читает
        всю таблицу. На каталоге из сотен позиций это незаметно,
        на миллионе — недопустимо, и там нужен полнотекстовый индекс
        (FTS5 в sqlite, GIN + tsvector в Postgres).
        """
        if not query.strip():
            return []
        pattern = f"%{query.strip()}%"
        rows = self.products.db.query_all(
            "SELECT * FROM products WHERE title LIKE ? ORDER BY title LIMIT ?",
            (pattern, limit),
        )
        from ..repositories.sqlite_repo import row_to_product

        return [row_to_product(r) for r in rows]
