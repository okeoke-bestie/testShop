"""Доменные исключения.

Зачем отдельная иерархия, а не HTTPException из FastAPI: бизнес-логика
не должна знать про HTTP. Она работает и из веб-ручки, и из CLI, и из
фоновой задачи, и из теста. Перевод в коды ответа — дело транспортного
слоя, и он собран в одном месте (`api.py`), а не размазан по сервисам.

Практическое следствие: чтобы отдать 409 вместо 500, достаточно бросить
доменное исключение — HTTP-слой сам разберётся.
"""

from __future__ import annotations


class ShopError(Exception):
    """Базовое исключение домена. Всё остальное наследуется от него."""

    code = "shop_error"
    http_status = 500

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFound(ShopError):
    code = "not_found"
    http_status = 404


class ValidationError(ShopError):
    code = "validation_error"
    http_status = 422


class NotAuthenticated(ShopError):
    """Не понятно, кто пришёл.

    401, а не 403: разница в том, что 401 означает «представьтесь»,
    а 403 — «вы представились, но вам нельзя». Клиент по этим кодам
    принимает разные решения: на 401 предлагает войти, на 403 —
    показывает отказ.
    """

    code = "not_authenticated"
    http_status = 401


class Forbidden(ShopError):
    """Покупатель известен, но действие ему недоступно.

    В этом проекте так отклоняется отзыв на товар, который человек
    не покупал.
    """

    code = "forbidden"
    http_status = 403


class BadCredentials(ShopError):
    """Неверная пара «почта + пароль».

    Сообщение намеренно не уточняет, что именно не совпало: разные
    ответы на «нет такого адреса» и «неверный пароль» превращают форму
    входа в проверялку зарегистрированных адресов.
    """

    code = "bad_credentials"
    http_status = 401


class EmailTaken(ShopError):
    """Адрес уже зарегистрирован.

    409, а не 422: запрос корректен, он конфликтует с существующими
    данными — тот же смысл, что у конфликта ключа идемпотентности.
    """

    code = "email_taken"
    http_status = 409


class DuplicateReview(ShopError):
    """Покупатель уже высказался об этом товаре.

    409, а не 422: запрос корректен, просто конфликтует с тем, что уже
    есть — тот же смысл, что у конфликта ключа идемпотентности.
    """

    code = "duplicate_review"
    http_status = 409


class OutOfStock(ShopError):
    """Товара не хватает на складе.

    Отдельный тип, а не общий ValidationError: это самая частая
    «ожидаемая» ошибка магазина, её считают отдельно и по-разному
    показывают пользователю.
    """

    code = "out_of_stock"
    http_status = 409


class TooManyRequests(ShopError):
    """Слишком часто.

    429, а не 403: отказ временный, и клиенту есть смысл повторить
    позже. Вместе с кодом отдаётся заголовок `Retry-After` — иначе
    вежливому клиенту остаётся только гадать, через сколько можно.
    """

    code = "too_many_requests"
    http_status = 429

    def __init__(self, message: str, retry_after: int = 60, **details):
        super().__init__(message, retry_after=retry_after, **details)
        self.retry_after = retry_after


class Conflict(ShopError):
    """Конкурентное изменение: кто-то успел раньше.

    Возникает при оптимистичной блокировке, когда версия записи
    изменилась между чтением и записью.
    """

    code = "conflict"
    http_status = 409


class IdempotencyConflict(ShopError):
    """Тот же ключ идемпотентности с другим телом запроса.

    Это ошибка клиента, а не гонка: он переиспользовал ключ для другого
    заказа. Молча вернуть старый результат нельзя — клиент получит
    не то, что просил, и не заметит.
    """

    code = "idempotency_conflict"
    http_status = 422


class ExternalServiceError(ShopError):
    """Внешний сервис недоступен или ответил ошибкой."""

    code = "external_service_error"
    http_status = 502
