#!/usr/bin/env python3
"""Переносит характеристики из data/specs.json в каталог.

Почему характеристики лежат отдельным файлом, а не прямо в каталоге:
у них другой ритм правок. Цены и остатки меняет человек регулярно,
характеристики — почти никогда. Держать их вместе значит каждый раз
пролистывать девять строк описания железа, чтобы поправить одну цену.

Для игр общая часть характеристик (платформа, жанр, студия, год)
не дублируется в файле, а берётся из полей каталога: данные, записанные
дважды, рано или поздно разойдутся.

Запуск:
    python scripts/build_catalog_specs.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "catalog.json"
SPECS = ROOT / "data" / "specs.json"

# Поля, которые для игры собираются из каталога.
FROM_CATALOG = [
    ("Платформа", "platform"),
    ("Жанр", "genre"),
    ("Разработчик", "developer"),
]


def build_specs(product: dict, extra: dict) -> dict:
    """Собирает итоговый набор: общая часть плюс частная из файла.

    Порядок важен: общие поля идут первыми, поэтому у всех игр начало
    таблицы одинаковое, и глазу не приходится искать строку заново
    на каждой карточке.
    """
    if product["category"] != "Игры":
        return dict(extra)

    specs = {label: product[field] for label, field in FROM_CATALOG if product.get(field)}
    if product.get("year"):
        specs["Год выхода"] = str(product["year"])
    # Частные поля НЕ перетирают общие, а дополняют: если в файле
    # случайно окажется «Жанр», источником истины остаётся каталог.
    for key, value in extra.items():
        specs.setdefault(key, value)
    return specs


def main() -> int:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    table = json.loads(SPECS.read_text(encoding="utf-8"))["specs"]

    missing = []
    for product in catalog["products"]:
        specs = build_specs(product, table.get(product["sku"], {}))
        if not specs:
            missing.append(product["sku"])
        product["specs"] = specs

    CATALOG.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if missing:
        print("без характеристик остались: " + ", ".join(missing))
        return 1

    counts = [len(p["specs"]) for p in catalog["products"]]
    print(f"характеристики проставлены: {len(catalog['products'])} товаров, "
          f"от {min(counts)} до {max(counts)} полей")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
