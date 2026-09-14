#!/usr/bin/env python3
"""Конвертация документации из markdown в обычный текст.

Зачем: .md открывается не везде и без подсветки читается плохо.
.txt открывается в любом блокноте на любой машине.

Markdown-файлы при этом остаются: GitHub и редакторы рендерят их
в удобный вид, и терять это незачем. Txt — это вторая копия для чтения,
а не замена.

Запуск:  python scripts/md_to_txt.py
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIDTH = 78


def strip_inline(text: str) -> str:
    """Убирает markdown-разметку внутри строки."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # ссылки
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", text)
    # Выделение, разорванное переносом строки, парными регулярками не ловится:
    # обработка идёт построчно, и открывающие ** остаются в одной строке,
    # а закрывающие — в другой. Убираем остатки отдельно.
    text = text.replace("**", "")
    return text


def convert_table(lines: list[str]) -> list[str]:
    """Markdown-таблица -> выровненные колонки."""
    rows: list[list[str]] = []
    for line in lines:
        if re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", line):
            continue  # строка-разделитель
        cells = [strip_inline(c.strip()) for c in line.strip().strip("|").split("|")]
        rows.append(cells)

    if not rows:
        return []

    n_cols = max(len(r) for r in rows)
    rows = [r + [""] * (n_cols - len(r)) for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(n_cols)]

    # Если таблица не влезает по ширине — печатаем по строкам «поле: значение».
    if sum(widths) + 3 * n_cols > WIDTH + 12:
        out: list[str] = []
        headers = rows[0]
        for row in rows[1:]:
            for header, cell in zip(headers, row, strict=True):
                if cell:
                    out.append(f"    {header}: {cell}")
            out.append("")
        return out

    out = []
    for i, row in enumerate(rows):
        out.append("  " + "  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)).rstrip())
        if i == 0:
            out.append("  " + "  ".join("-" * w for w in widths))
    return out


def convert(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    table_buffer: list[str] = []

    def flush_table() -> None:
        if table_buffer:
            out.extend(convert_table(table_buffer))
            out.append("")
            table_buffer.clear()

    for raw in lines:
        line = raw.rstrip()

        if line.strip().startswith("```"):
            flush_table()
            in_code = not in_code
            out.append("")
            continue

        if in_code:
            out.append("    " + raw)
            continue

        if "|" in line and line.strip().startswith("|"):
            table_buffer.append(line)
            continue
        flush_table()

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            title = strip_inline(heading.group(2))
            out.append("")
            if level == 1:
                out.append("=" * WIDTH)
                out.append(title.upper())
                out.append("=" * WIDTH)
            elif level == 2:
                out.append(title)
                out.append("-" * min(len(title), WIDTH))
            else:
                out.append(f"* {title}")
            out.append("")
            continue

        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            out.append("")
            out.append("-" * WIDTH)
            out.append("")
            continue

        if not line.strip():
            out.append("")
            continue

        bullet = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
        if bullet:
            indent = len(bullet.group(1))
            marker = "-" if bullet.group(2) in "-*+" else bullet.group(2)
            body = strip_inline(bullet.group(3))
            prefix = " " * (2 + indent) + marker + " "
            out.extend(
                textwrap.wrap(
                    body,
                    width=WIDTH,
                    initial_indent=prefix,
                    subsequent_indent=" " * len(prefix),
                )
                or [prefix.rstrip()]
            )
            continue

        if line.startswith(">"):
            body = strip_inline(line.lstrip("> ").strip())
            out.extend(
                textwrap.wrap(body, width=WIDTH, initial_indent="  | ", subsequent_indent="  | ")
            )
            continue

        body = strip_inline(line)
        indent = len(line) - len(line.lstrip())
        out.extend(
            textwrap.wrap(
                body,
                width=WIDTH,
                initial_indent=" " * indent,
                subsequent_indent=" " * indent,
            )
        )

    flush_table()

    # Схлопываем тройные пустые строки
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def main() -> int:
    targets = sorted(
        set(ROOT.glob("*.md")) | set(ROOT.glob("docs/**/*.md"))
    )
    if not targets:
        print("markdown-файлы не найдены", file=sys.stderr)
        return 1

    for path in targets:
        txt_path = path.with_suffix(".txt")
        txt_path.write_text(convert(path.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"{path.relative_to(ROOT)}  ->  {txt_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
