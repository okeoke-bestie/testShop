"""Тесты разбора .env.

Мелочь на вид, но именно через неё приложение узнаёт, в какую базу
ходить. Ошибка здесь означает «работал не с той базой» — поэтому
правила проверены явно, а не на глаз.
"""

from __future__ import annotations

import os

from shopapi.env_file import load, parse


def test_parses_simple_pairs():
    values = parse("DATABASE_URL=postgresql://a@b/c\nPORT=8000\n")
    assert values == {"DATABASE_URL": "postgresql://a@b/c", "PORT": "8000"}


def test_ignores_comments_and_blanks():
    values = parse("# комментарий\n\n  # ещё\nA=1\n")
    assert values == {"A": "1"}


def test_strips_quotes_and_export():
    values = parse('export A="1"\nB=\'2\'\n')
    assert values == {"A": "1", "B": "2"}


def test_keeps_special_characters_in_value():
    """Пароль и query-строка не должны пострадать при разборе."""
    values = parse("DATABASE_URL=postgresql://u:p@ss=word@h:5432/db?sslmode=require\n")
    assert values["DATABASE_URL"].endswith("?sslmode=require")
    assert "p@ss=word" in values["DATABASE_URL"]


def test_lines_without_equals_are_skipped():
    assert parse("мусор\nA=1\n") == {"A": "1"}


def test_environment_wins_over_file(tmp_path, monkeypatch):
    """Главное правило: настоящее окружение важнее файла.

    Иначе забытая строчка в .env будет молча перебивать то, что задано
    при запуске — и человек будет чинить не ту базу.
    """
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "from-env")

    applied = load(env)

    assert applied == {}
    assert os.environ["DATABASE_URL"] == "from-env"


def test_file_fills_in_what_environment_lacks(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=from-file\n", encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    applied = load(env)

    assert applied == {"DATABASE_URL": "from-file"}
    assert os.environ["DATABASE_URL"] == "from-file"


def test_missing_file_is_not_an_error(tmp_path):
    assert load(tmp_path / "нет-такого") == {}
