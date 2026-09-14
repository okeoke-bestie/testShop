"""Загрузка демонстрационных отзывов из файла.

Тот же подход, что и у каталога: данные лежат текстом, код их проверяет
и заливает. Отличие одно и существенное — эти отзывы ВЫДУМАНЫ, магазин
учебный, покупателей нет. Поэтому каждая запись уходит в базу с флагом
`is_demo`, витрина этот флаг показывает, и удалить их можно одной
командой. Демо-данные, которые невозможно отличить от настоящих, —
это ловушка, в которую рано или поздно попадёт кто-то ещё.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import ValidationError


@dataclass(frozen=True)
class ReviewItem:
    author: str
    rating: int
    body: str
    title: str = ""
    sku: str | None = None
    email: str | None = None   # связь с профилем покупателя


@dataclass(frozen=True)
class CustomerItem:
    name: str
    email: str
    city: str = ""


def load_customers(path: str | Path) -> list[CustomerItem]:
    """Читает профили покупателей.

    Дубли по почте отсеиваются здесь, а не в базе: на колонке стоит
    UNIQUE, и заливка упала бы на середине, оставив половину данных.
    Проверить заранее дешевле, чем разбирать частично залитую базу.
    """
    path = Path(path)
    if not path.exists():
        raise ValidationError(f"файл покупателей не найден: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_items = payload.get("customers") if isinstance(payload, dict) else payload

    items: list[CustomerItem] = []
    problems: list[str] = []
    seen: set[str] = set()

    for index, raw in enumerate(raw_items or [], start=1):
        name = str(raw.get("name", "")).strip()
        email = str(raw.get("email", "")).strip().lower()
        if not name:
            problems.append(f"покупатель {index}: не заполнено имя")
            continue
        if "@" not in email:
            problems.append(f"покупатель {index} ({name}): некорректная почта")
            continue
        if email in seen:
            problems.append(f"покупатель {index}: почта {email} повторяется")
            continue
        seen.add(email)
        items.append(CustomerItem(name=name, email=email,
                                  city=str(raw.get("city", "")).strip()))

    if problems:
        raise ValidationError(
            "в файле покупателей есть ошибки:\n  - " + "\n  - ".join(problems),
            path=str(path), count=len(problems),
        )
    return items


def seed_customers(customers_repo, items: list[CustomerItem]) -> int:
    """Заливает демо-профили. Повторный запуск не плодит копии."""
    customers_repo.delete_demo()
    now = datetime.now(timezone.utc)
    with customers_repo.db.transaction():
        for index, item in enumerate(items):
            customers_repo.add(
                name=item.name, email=item.email, city=item.city, is_demo=True,
                # Даты регистрации разнесены на пару лет назад: профиль,
                # созданный сегодня, и отзыв годовой давности — заметное
                # противоречие.
                joined_at=now - timedelta(days=90 + index * 5),
            )
    return len(items)


def load_reviews(path: str | Path) -> list[ReviewItem]:
    """Читает файл отзывов, собирая все ошибки сразу."""
    path = Path(path)
    if not path.exists():
        raise ValidationError(f"файл отзывов не найден: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"файл отзывов — не корректный JSON: строка {exc.lineno}, {exc.msg}",
            path=str(path),
        ) from exc

    raw_items = payload.get("reviews") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        raise ValidationError("в файле отзывов нет списка reviews", path=str(path))

    items: list[ReviewItem] = []
    problems: list[str] = []

    for index, raw in enumerate(raw_items, start=1):
        where = f"отзыв {index}"
        if not isinstance(raw, dict):
            problems.append(f"{where}: ожидался объект")
            continue
        author = str(raw.get("author", "")).strip()
        body = str(raw.get("body", "")).strip()
        rating = raw.get("rating")
        if not author:
            problems.append(f"{where}: не заполнен author")
            continue
        if not body:
            problems.append(f"{where} ({author}): не заполнен body")
            continue
        if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
            problems.append(f"{where} ({author}): rating должен быть целым от 1 до 5")
            continue
        sku = raw.get("sku")
        email = raw.get("email")
        items.append(ReviewItem(
            author=author, rating=rating, body=body,
            title=str(raw.get("title", "")).strip(),
            sku=str(sku).strip() if sku else None,
            email=str(email).strip().lower() if email else None,
        ))

    if problems:
        raise ValidationError(
            "в файле отзывов есть ошибки:\n  - " + "\n  - ".join(problems),
            path=str(path), count=len(problems),
        )
    return items


def seed_reviews(reviews_repo, products_repo, items: list[ReviewItem],
                 customers_repo=None) -> int:
    """Заливает демо-отзывы, связывая их с товарами по артикулу.

    Сопоставление артикулов делается ОДНИМ запросом, а не поиском товара
    на каждый отзыв: пятнадцать отзывов — это пятнадцать лишних походов
    в базу, а на тысяче их будет тысяча. Ровно та же мысль, что и
    в `list_for_customer` у заказов.

    Отзыв с неизвестным артикулом не выбрасывается, а сохраняется без
    привязки: текст написал человек, и терять его из-за опечатки
    в служебном поле неправильно.
    """
    reviews_repo.delete_demo()  # повторный запуск не должен плодить копии

    rows = products_repo.db.query_all("SELECT id, sku FROM products")
    id_by_sku = {row["sku"]: row["id"] for row in rows}

    # Профили тоже подтягиваются одним запросом, а не поиском по одному
    # на отзыв: шестьсот отзывов — это шестьсот лишних походов в базу.
    id_by_email: dict[str, int] = {}
    if customers_repo is not None:
        people = customers_repo.db.query_all("SELECT id, email FROM customers")
        id_by_email = {row["email"]: row["id"] for row in people}

    # Даты разносятся по прошедшим неделям. Если залить все отзывы
    # одним моментом, на витрине у пятнадцати отзывов будет одинаковое время
    # до минуты — сразу видно, что данные сгенерированы, и проверить
    # сортировку по дате на таких данных невозможно.
    # Отсчёт от полудня, а сдвиг внутри дня — не больше десяти часов.
    # Иначе часовая добавка переносит часть отзывов на соседний день,
    # даты совпадают, и «у каждого отзыва своя дата» перестаёт
    # выполняться. Поймано тестом test_seeding_spreads_dates.
    noon = datetime.now(timezone.utc).replace(hour=12, minute=0,
                                              second=0, microsecond=0)

    with reviews_repo.db.transaction():
        for index, item in enumerate(items):
            reviews_repo.add(
                author=item.author,
                rating=item.rating,
                body=item.body,
                title=item.title,
                product_id=id_by_sku.get(item.sku) if item.sku else None,
                is_demo=True,
                created_at=noon - timedelta(days=index % 330 + 1,
                                            minutes=index * 37 % 600),
                customer_id=id_by_email.get(item.email) if item.email else None,
                # Демонстрационные отзывы считаются подтверждёнными:
                # предполагается, что человек товар купил. Настоящий
                # отзыв получает этот флаг только после проверки заказа.
                verified=bool(item.email and item.sku),
            )
    return len(items)
