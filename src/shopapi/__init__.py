"""shopapi — REST-сервис заказов небольшого магазина."""

from .db import Database
from .errors import Conflict, NotFound, OutOfStock, ShopError, ValidationError
from .models import Order, OrderLine, OrderStatus, Page, Product
from .reliability import CircuitBreaker, RetryPolicy, TTLCache, with_retry
from .repositories.sqlite_repo import SqliteOrderRepository, SqliteProductRepository
from .services.catalog import CatalogService
from .services.orders import OrderRequest, OrderService

__version__ = "1.0.0"

__all__ = [
    "CatalogService", "CircuitBreaker", "Conflict", "Database", "NotFound",
    "Order", "OrderLine", "OrderRequest", "OrderService", "OrderStatus",
    "OutOfStock", "Page", "Product", "RetryPolicy", "ShopError",
    "SqliteOrderRepository", "SqliteProductRepository", "TTLCache",
    "ValidationError", "with_retry",
]
