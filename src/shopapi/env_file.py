"""Чтение .env — чтобы не воевать с переменными окружения Windows.

Настройка подключения к базе обязана жить вне кода: одна и та же сборка
должна уметь ходить и в локальную базу, и в продовую, а строка
подключения содержит пароль и не должна попадать в репозиторий.
Канонический способ — переменная окружения, и она здесь главная.

Но задать переменную окружения в IDE на Windows — отдельное упражнение,
поэтому рядом поддерживается файл `.env`: его правят текстом, он
в `.gitignore`, и он **не перекрывает** уже заданное окружение.
Приоритет: настоящее окружение -> .env -> значения по умолчанию.
Именно в таком порядке, иначе забытая строчка в файле будет тихо
перебивать то, что задано при запуске.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"


def parse(text: str) -> dict[str, str]:
    """Разбирает содержимое .env.

    Формат намеренно минимальный: KEY=value, решётка — комментарий,
    кавычки вокруг значения снимаются. Никакой подстановки переменных
    и никакого выполнения кода: .env — это данные, а не скрипт.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Подкладывает значения из .env в os.environ.

    Возвращает то, что реально применилось — чтобы команда могла
    честно сказать пользователю, откуда взялся DATABASE_URL.
    """
    path = path or ENV_FILE
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    applied: dict[str, str] = {}
    for key, value in parse(text).items():
        if key in os.environ and not override:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


def source_of(key: str, path: Path | None = None) -> str:
    """Откуда пришло значение: 'окружение', '.env' или 'не задано'."""
    path = path or ENV_FILE
    in_file = key in parse(path.read_text(encoding="utf-8")) if path.exists() else False
    if key not in os.environ:
        return "не задано"
    return ".env" if in_file else "окружение"
