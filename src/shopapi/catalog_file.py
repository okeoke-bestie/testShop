"""Загрузка каталога из JSON-файла.

Зачем отдельный файл с данными, а не список в коде: товары — это данные,
а не логика. Их правит человек, который не открывает Python-файлы,
и каждая правка не должна требовать перезапуска разработчика.

Файл читается с проверками: ошибка в данных должна называть строку
и поле, а не падать с `KeyError: 'stock'` где-то в глубине.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError


@dataclass(frozen=True)
class CatalogItem:
    """Позиция каталога, как она записана в файле."""

    sku: str
    title: str
    price_kopecks: int
    stock: int
    description: str = ""
    category: str = "Прочее"
    emoji: str = "📦"
    # Справочные поля витрины. Со значениями по умолчанию — чтобы
    # старый файл каталога (без них) продолжал читаться: формат данных
    # ломать нельзя, даже когда данные свои.
    platform: str = ""
    genre: str = ""
    developer: str = ""
    year: int = 0
    # Характеристики хранятся СТРОКОЙ с JSON, а не словарём: в базе это
    # одна текстовая колонка, и превращать словарь в строку лучше один
    # раз здесь, чем в каждом репозитории по отдельности.
    specs: str = "{}"


def rub_to_kopecks(value) -> int:
    """Рубли из файла в копейки для хранения.

    Почему через строку, а не `int(value * 100)`: умножение float на 100
    даёт классические артефакты — `4999.90 * 100` это 499989.99999999994,
    и `int()` от него вернёт 499989, то есть на копейку меньше. Округление
    до целого решает задачу, но лучше сразу считать десятичными.
    """
    from decimal import Decimal, InvalidOperation

    try:
        kopecks = (Decimal(str(value)) * 100).quantize(Decimal("1"))
    except (InvalidOperation, TypeError) as exc:
        raise ValidationError(f"некорректная цена: {value!r}") from exc
    if kopecks < 0:
        raise ValidationError(f"цена не может быть отрицательной: {value!r}")
    return int(kopecks)


def load_catalog(path: str | Path) -> list[CatalogItem]:
    """Читает каталог и проверяет каждую позицию.

    Ошибки собираются ВСЕ сразу, а не по одной: иначе человек чинит файл
    по одной строке за запуск. Это то же соображение, по которому сервис
    сообщает обо всех отсутствующих товарах заказа сразу.
    """
    path = Path(path)
    if not path.exists():
        raise ValidationError(f"файл каталога не найден: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"файл каталога — не корректный JSON: строка {exc.lineno}, {exc.msg}",
            path=str(path),
        ) from exc

    raw_items = payload.get("products") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        raise ValidationError("в файле каталога нет списка products", path=str(path))

    items: list[CatalogItem] = []
    problems: list[str] = []
    seen_skus: set[str] = set()

    for index, raw in enumerate(raw_items, start=1):
        where = f"позиция {index}"
        if not isinstance(raw, dict):
            problems.append(f"{where}: ожидался объект, получено {type(raw).__name__}")
            continue

        sku = str(raw.get("sku", "")).strip()
        title = str(raw.get("title", "")).strip()
        if not sku:
            problems.append(f"{where}: не заполнен sku")
            continue
        if not title:
            problems.append(f"{where} ({sku}): не заполнен title")
            continue
        if sku in seen_skus:
            # Дубль артикула упал бы на уникальном индексе уже в базе,
            # но сообщение оттуда человеку ничего не скажет.
            problems.append(f"{where}: артикул {sku} повторяется")
            continue
        seen_skus.add(sku)

        try:
            price = rub_to_kopecks(raw.get("price_rub", raw.get("price", 0)))
        except ValidationError as exc:
            problems.append(f"{where} ({sku}): {exc.message}")
            continue

        stock_raw = raw.get("stock", 0)
        if not isinstance(stock_raw, int) or isinstance(stock_raw, bool):
            problems.append(f"{where} ({sku}): stock должен быть целым числом")
            continue
        if stock_raw < 0:
            problems.append(f"{where} ({sku}): stock не может быть отрицательным")
            continue

        year_raw = raw.get("year", 0)
        if not isinstance(year_raw, int) or isinstance(year_raw, bool):
            problems.append(f"{where} ({sku}): year должен быть целым числом")
            continue
        if year_raw and not (1970 <= year_raw <= 2100):
            # Опечатка в годе (20223) — это не «просто текст в карточке»:
            # по нему сортируют, и она уедет в самый верх выдачи.
            problems.append(f"{where} ({sku}): год {year_raw} вне разумного диапазона")
            continue

        items.append(
            CatalogItem(
                sku=sku,
                title=title,
                price_kopecks=price,
                stock=stock_raw,
                description=str(raw.get("description", "")).strip(),
                category=str(raw.get("category", "Прочее")).strip() or "Прочее",
                emoji=str(raw.get("emoji", "📦")).strip() or "📦",
                platform=str(raw.get("platform", "")).strip(),
                genre=str(raw.get("genre", "")).strip(),
                developer=str(raw.get("developer", "")).strip(),
                year=year_raw,
                specs=json.dumps(raw.get("specs", {}), ensure_ascii=False),
            )
        )

    if problems:
        raise ValidationError(
            "в файле каталога есть ошибки:\n  - " + "\n  - ".join(problems),
            path=str(path),
            count=len(problems),
        )
    if not items:
        raise ValidationError("каталог пуст", path=str(path))
    return items


def seed_database(products_repo, items: list[CatalogItem], replace: bool = True) -> int:
    """Заливает каталог в базу.

    `replace=True` очищает товары перед загрузкой: файл — источник
    истины, и после правки в базе должно оказаться ровно то, что в нём.
    Иначе удалённая из файла позиция осталась бы в базе навсегда.

    Заказы при этом не трогаются: у них внешние ключи на товары,
    и чистка каталога сломала бы историю.
    """
    db = products_repo.db
    with db.transaction():
        if replace:
            existing_orders = db.query_one("SELECT COUNT(*) AS n FROM order_lines")
            if existing_orders and existing_orders["n"]:
                raise ValidationError(
                    "в базе есть заказы, ссылающиеся на товары — "
                    "удалите файл базы целиком и создайте заново"
                )
            db.execute("DELETE FROM products")
        for item in items:
            # Вызов один и тот же для обеих баз: у репозиториев одинаковая
            # сигнатура. Раньше здесь стоял `except TypeError` с урезанным
            # повтором — на случай, если sqlite-версия не примет описание
            # и категорию. Этот «на всякий случай» и прятал настоящую
            # проблему: схемы разошлись, витрина работала только
            # на Postgres, а seed молча заливал товары без описаний.
            # Заодно такой except проглотил бы TypeError откуда угодно
            # изнутри репозитория.
            products_repo.add(
                item.sku, item.title, item.price_kopecks, item.stock,
                item.description, item.category, item.emoji,
                item.platform, item.genre, item.developer, item.year, item.specs,
            )
    return len(items)
