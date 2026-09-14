"""Пароли и сессии.

Раньше покупатель выбирался из списка — это было честно названо
«не аутентификация». Теперь она настоящая, и вот что в ней важно.

**Пароль не хранится.** Хранится результат `scrypt` — функции, которую
специально сделали медленной и требовательной к памяти. Подбор по
украденной базе становится дорогим: на каждую попытку нужны те же
десятки мегабайт и то же время, что и при проверке.

`scrypt` берётся из стандартной библиотеки (`hashlib`), без внешних
зависимостей. Это не компромисс: в OpenSSL он реализован нормально,
и для учебного проекта это правильный выбор. В рабочем сервисе взял бы
argon2id через `argon2-cffi` — он новее и устойчивее к подбору
на видеокартах.

**Соль у каждого своя.** Одинаковые пароли дают разные хеши, поэтому
радужные таблицы бесполезны, а по базе не видно, у кого пароли
совпадают.

**Сравнение — постоянное по времени.** `hmac.compare_digest` вместо
`==`: обычное сравнение выходит на первом различающемся байте, и по
времени ответа можно посимвольно угадывать значение.

**В базе лежит хеш токена, а не сам токен.** Утечка таблицы сессий
не даёт войти: из хеша токен не восстановить. Ровно та же мысль,
что и с паролем.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Параметры scrypt. n — число итераций, r и p — блок и параллелизм.
# 2**14 при r=8 требует около 16 МБ памяти на проверку: заметно для
# подбора и незаметно для входа одного человека.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LENGTH = 32
SALT_BYTES = 16

MIN_PASSWORD = 8
MAX_PASSWORD = 200

# Срок жизни сессии. Бесконечные токены — плохая идея: украденный
# однажды работает вечно.
SESSION_DAYS = 30


@dataclass(frozen=True)
class PasswordProblem:
    """Ошибки пароля собираются все сразу, как и везде в проекте."""

    problems: list[str]


def check_password_strength(password: str) -> list[str]:
    """Минимальные требования — и объяснимые.

    Не требуем спецсимволов и цифр: правила вида «заглавная, цифра
    и знак» заставляют людей писать `Password1!`, что подбирается
    быстрее длинной фразы. Длина важнее состава.
    """
    problems = []
    if len(password) < MIN_PASSWORD:
        problems.append(f"пароль короче {MIN_PASSWORD} символов")
    if len(password) > MAX_PASSWORD:
        problems.append(f"пароль длиннее {MAX_PASSWORD} символов")
    if password.strip() != password:
        problems.append("пароль начинается или заканчивается пробелом")
    return problems


def hash_password(password: str) -> str:
    """Возвращает строку вида `scrypt$соль$хеш` — всё в одном поле.

    Алгоритм записан ВНУТРЬ строки намеренно: когда завтра параметры
    изменятся или scrypt заменят на argon2, старые хеши останутся
    проверяемыми, а новые начнут писаться по-новому. Без этого
    смена алгоритма означает сброс паролей у всех.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Проверка пароля.

    Пустое значение означает профиль без пароля — такие есть
    у демонстрационных покупателей, и войти под ними нельзя.
    """
    if not stored:
        return False
    try:
        algorithm, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algorithm != "scrypt":
        return False

    expected = bytes.fromhex(digest_hex)
    actual = _scrypt(password, bytes.fromhex(salt_hex))
    # compare_digest, а не ==: обычное сравнение выходит на первом
    # различии, и по времени ответа значение подбирается побайтно.
    return hmac.compare_digest(expected, actual)


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_LENGTH,
    )


# ---------------------------------------------------------------- сессии

def new_token() -> str:
    """Случайный токен сессии.

    `secrets`, а не `random`: второй предсказуем по нескольким
    значениям, и токены из него подбираются. Для всего, что защищает
    доступ, годится только криптографический генератор.
    """
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """В базу уходит хеш, а не токен.

    Здесь достаточно обычного sha256, без соли и замедления: токен
    случайный и длинный, перебирать его бессмысленно. Медленная
    функция нужна там, где секрет выбирает человек.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(days: int = SESSION_DAYS) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)
