"""Доменные модели.

Обычные dataclass'ы, без ORM и без Pydantic. Причина: это ядро,
и оно не должно зависеть ни от базы, ни от веб-фреймворка. Pydantic-схемы
живут в `api.py` и описывают формат HTTP-запросов, ORM-строки — в
репозитории. Между собой слои общаются вот этими типами.

Проверка на практике: тесты бизнес-логики запускаются без базы и без
сети за миллисекунды именно потому, что домен ни от чего не зависит.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    """Время в UTC, всегда с таймзоной.

    `datetime.utcnow()` возвращает naive-объект без таймзоны — источник
    классических багов: сравнение naive и aware падает, а разница между
    сервером и базой в часовых поясах обнаруживается уже в проде.
    """
    return datetime.now(timezone.utc)


class OrderStatus(str, Enum):
    """Статусы заказа.

    Наследование от str делает значения сериализуемыми как есть:
    `json.dumps` и sqlite принимают их без преобразования.
    """

    NEW = "new"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Product:
    """Товар каталога.

    frozen=True: товар в памяти не мутируется. Изменение цены или остатка
    идёт через репозиторий и порождает новый объект. Это дешёвая защита
    от целого класса ошибок, когда объект поменяли в одном месте,
    а другое место продолжает держать ссылку и не знает об этом.
    """

    id: int
    sku: str
    title: str
    price_kopecks: int
    stock: int
    version: int = 1

    def __post_init__(self) -> None:
        if self.price_kopecks < 0:
            raise ValueError("цена не может быть отрицательной")
        if self.stock < 0:
            raise ValueError("остаток не может быть отрицательным")

    @property
    def price_rub(self) -> str:
        """Цена для показа человеку.

        Деньги хранятся в копейках целым числом. float для денег
        не годится: 0.1 + 0.2 != 0.3, и на тысячах операций расхождение
        становится видимым в отчётах.
        """
        return f"{self.price_kopecks // 100}.{self.price_kopecks % 100:02d}"


@dataclass(frozen=True)
class OrderLine:
    """Строка заказа.

    Цена копируется в строку заказа НАМЕРЕННО, а не берётся из товара
    по ссылке: цена в каталоге меняется, а заказ должен навсегда помнить,
    по какой цене его оформили. Иначе вчерашние заказы начнут
    пересчитываться при каждом изменении прайса.
    """

    product_id: int
    sku: str
    quantity: int
    price_kopecks: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("количество должно быть положительным")

    @property
    def total_kopecks(self) -> int:
        return self.quantity * self.price_kopecks


@dataclass
class Order:
    id: int | None
    customer_id: int
    lines: list[OrderLine] = field(default_factory=list)
    status: OrderStatus = OrderStatus.NEW
    created_at: datetime = field(default_factory=utcnow)
    idempotency_key: str | None = None

    @property
    def total_kopecks(self) -> int:
        return sum(line.total_kopecks for line in self.lines)

    def can_cancel(self) -> bool:
        return self.status in {OrderStatus.NEW, OrderStatus.PAID}


@dataclass(frozen=True)
class Page:
    """Страница выдачи с курсором.

    `next_cursor is None` означает, что данные закончились. Клиент
    не должен догадываться об этом по «вернулось меньше, чем просил»:
    это неверно, когда часть записей отфильтрована на стороне базы.
    """

    items: list
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None
