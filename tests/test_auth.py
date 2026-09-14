"""Регистрация, вход и сессии.

Раньше покупатель выбирался из списка, и «войти кем угодно» означало
поменять одно число в заголовке. Эти тесты закрепляют, что так больше
нельзя, и заодно фиксируют решения, которые легко потерять при правке:
одинаковый ответ на неверный пароль и несуществующий адрес, срок жизни
сессии, хеш токена в базе.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from shopapi.auth import (
    check_password_strength,
    hash_password,
    new_token,
    token_hash,
    verify_password,
)
from shopapi.errors import BadCredentials, EmailTaken, ValidationError
from shopapi.repositories.customers_repo import SqliteCustomerRepository


@pytest.fixture
def customers(db):
    return SqliteCustomerRepository(db)


# ---------- хеширование пароля ----------

def test_password_is_not_stored_as_text():
    stored = hash_password("moy-sekretnyy-parol")
    assert "moy-sekretnyy-parol" not in stored
    assert stored.startswith("scrypt$")


def test_same_password_gives_different_hashes():
    """Своя соль у каждого: по базе не видно, у кого пароли совпадают,
    и радужные таблицы бесполезны."""
    assert hash_password("odin-i-tot-zhe") != hash_password("odin-i-tot-zhe")


def test_verify_accepts_right_and_rejects_wrong():
    stored = hash_password("pravilnyy-parol")
    assert verify_password("pravilnyy-parol", stored) is True
    assert verify_password("nepravilnyy-parol", stored) is False


def test_verify_rejects_empty_and_broken_hash():
    """Профиль без пароля (демонстрационный) войти не позволяет."""
    assert verify_password("любой", None) is False
    assert verify_password("любой", "") is False
    assert verify_password("любой", "мусор") is False
    assert verify_password("любой", "md5$aa$bb") is False, "чужой алгоритм не принимается"


def test_algorithm_is_stored_inside_the_hash():
    """Алгоритм записан в строке — его можно сменить, не сбрасывая
    пароли всем пользователям."""
    assert hash_password("x" * 10).split("$")[0] == "scrypt"


@pytest.mark.parametrize("password,expected", [
    ("короткий", True),        # 8 символов — ровно граница
    ("1234567", False),
    ("  пароль с пробелом  ", False),
])
def test_password_strength(password, expected):
    assert (check_password_strength(password) == []) is expected


# ---------- токены ----------

def test_tokens_are_unique_and_long():
    tokens = {new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 32 for t in tokens)


def test_token_hash_is_one_way():
    token = new_token()
    assert token not in token_hash(token)


# ---------- регистрация ----------

def test_register_creates_profile(customers):
    customer_id = customers.register(
        name="Даниил", email="D@Example.COM", password="dlinnyy-parol", city="Москва"
    )
    row = customers.get(customer_id)
    assert row["name"] == "Даниил"
    assert row["email"] == "d@example.com", "почта приводится к нижнему регистру"
    assert row["is_demo"] == 0


def test_registered_password_is_not_readable(customers):
    customers.register(name="Аня", email="a@example.com", password="moy-parol-123")
    row = customers.find_by_email("a@example.com")
    assert "moy-parol-123" not in str(dict(row))


def test_duplicate_email_is_rejected(customers):
    """Уникальность обеспечивает база, а не проверка в коде: два
    параллельных запроса оба увидели бы, что адрес свободен."""
    customers.register(name="Аня", email="a@example.com", password="dlinnyy-parol")
    with pytest.raises(EmailTaken):
        customers.register(name="Другая", email="A@EXAMPLE.COM", password="drugoy-parol")


def test_short_password_is_rejected(customers):
    with pytest.raises(ValidationError, match="пароль"):
        customers.register(name="Аня", email="a@example.com", password="123")


def test_bad_email_is_rejected(customers):
    with pytest.raises(ValidationError, match="почта"):
        customers.register(name="Аня", email="не-почта", password="dlinnyy-parol")


# ---------- вход ----------

def test_login_with_right_password(customers):
    customers.register(name="Аня", email="a@example.com", password="dlinnyy-parol")
    row = customers.authenticate("A@example.com", "dlinnyy-parol")
    assert row["name"] == "Аня"


def test_login_with_wrong_password_fails(customers):
    customers.register(name="Аня", email="a@example.com", password="dlinnyy-parol")
    with pytest.raises(BadCredentials):
        customers.authenticate("a@example.com", "ne-tot-parol")


def test_unknown_email_gives_the_same_error(customers):
    """Одинаковый ответ — иначе форма входа превращается в проверялку
    зарегистрированных адресов: подставляя почту, узнаёшь, кто есть."""
    customers.register(name="Аня", email="a@example.com", password="dlinnyy-parol")

    with pytest.raises(BadCredentials) as unknown:
        customers.authenticate("нет@example.com", "dlinnyy-parol")
    with pytest.raises(BadCredentials) as wrong:
        customers.authenticate("a@example.com", "ne-tot-parol")

    assert unknown.value.message == wrong.value.message
    assert unknown.value.code == wrong.value.code


def test_demo_customer_cannot_log_in(customers):
    """У демонстрационных профилей пароля нет — войти под ними нельзя."""
    customers.add(name="Демо", email="demo@example.com", is_demo=True)
    with pytest.raises(BadCredentials):
        customers.authenticate("demo@example.com", "любой пароль")


# ---------- сессии ----------

def test_session_token_identifies_customer(customers):
    customer_id = customers.register(
        name="Аня", email="a@example.com", password="dlinnyy-parol"
    )
    token = customers.start_session(customer_id)

    row = customers.customer_by_token(token)

    assert row is not None
    assert row["id"] == customer_id


def test_raw_token_is_not_in_the_database(customers, db):
    """В базе лежит хеш: утечка таблицы сессий не даёт войти."""
    customer_id = customers.register(
        name="Аня", email="a@example.com", password="dlinnyy-parol"
    )
    token = customers.start_session(customer_id)

    rows = db.query_all("SELECT token_hash FROM sessions")

    assert all(row["token_hash"] != token for row in rows)
    assert rows[0]["token_hash"] == token_hash(token)


def test_unknown_token_gives_nothing(customers):
    assert customers.customer_by_token("выдуманный-токен") is None
    assert customers.customer_by_token("") is None


def test_logout_kills_the_session(customers):
    customer_id = customers.register(
        name="Аня", email="a@example.com", password="dlinnyy-parol"
    )
    token = customers.start_session(customer_id)

    customers.end_session(token)

    assert customers.customer_by_token(token) is None


def test_expired_session_does_not_work(customers, db):
    """Срок проверяется в запросе, а не в Python."""
    customer_id = customers.register(
        name="Аня", email="a@example.com", password="dlinnyy-parol"
    )
    token = customers.start_session(customer_id)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.execute("UPDATE sessions SET expires_at = ?", (past,))

    assert customers.customer_by_token(token) is None


def test_purge_removes_only_expired(customers, db):
    alive_id = customers.register(name="Аня", email="a@example.com", password="dlinnyy-parol")
    alive = customers.start_session(alive_id)
    dead = customers.start_session(alive_id)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
               (past, token_hash(dead)))

    removed = customers.purge_expired_sessions()

    assert removed == 1
    assert customers.customer_by_token(alive) is not None


def test_several_sessions_live_side_by_side(customers):
    """Вход с телефона не должен выбрасывать из браузера."""
    customer_id = customers.register(
        name="Аня", email="a@example.com", password="dlinnyy-parol"
    )
    first = customers.start_session(customer_id)
    second = customers.start_session(customer_id)

    customers.end_session(first)

    assert customers.customer_by_token(first) is None
    assert customers.customer_by_token(second) is not None


# ---------- интерфейсы двух баз ----------

def test_auth_methods_agree_on_signatures():
    pytest.importorskip("psycopg", reason="psycopg не установлен")
    from shopapi.repositories.customers_repo import PgCustomerRepository

    for name in ["register", "authenticate", "start_session",
                 "customer_by_token", "end_session", "purge_expired_sessions"]:
        ours = inspect.signature(getattr(SqliteCustomerRepository, name))
        theirs = inspect.signature(getattr(PgCustomerRepository, name))
        assert list(ours.parameters) == list(theirs.parameters), name
