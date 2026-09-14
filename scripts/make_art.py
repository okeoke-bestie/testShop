#!/usr/bin/env python3
"""Рисует обложки товаров — собственной графикой, а не чужими картинками.

Почему так, а не «скачать настоящие обложки»: обложки игр и фотографии
консолей принадлежат издателям и производителям. Класть их в учебный
проект нельзя, и подменять вопрос серыми квадратами тоже не хочется.

Поэтому обложка собирается из геометрии, а вид её задаёт ЖАНР: у шутера
резкие диагонали и горячая палитра, у приключения — мягкие дуги и холодная,
у гонок — полосы скорости. Цвет берётся детерминированно из артикула,
поэтому один товар всегда выглядит одинаково, а рядом стоящие карточки
не сливаются.

Запуск:
    python scripts/make_art.py            # нарисовать всё заново
    python scripts/make_art.py --check    # проверить, что для всех есть файл

Результат — SVG в src/shopapi/web/art/<sku>.svg. Векторные файлы весят
единицы килобайт, не мылятся на экранах высокой плотности и правятся
текстом, а не редактором.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "catalog.json"
ART_DIR = ROOT / "src" / "shopapi" / "web" / "art"

W, H = 640, 800  # пропорции коробки с игрой

# Палитры по платформам: покупатель должен различать полку глазами,
# не читая подписи. Оттенки подобраны руками — производная от хеша
# даёт ядовитые сочетания, это уже проверено на прошлой версии витрины.
PLATFORM_PALETTES = {
    "PlayStation 5":   [("#0b1a3a", "#1f4fd8"), ("#101c46", "#3b6fe8"), ("#0a1430", "#2a5cff")],
    "Xbox Series X|S": [("#06240f", "#12a04a"), ("#052b14", "#1fbf5c"), ("#0a2a12", "#0f8f3f")],
    "Nintendo Switch": [("#3a0b12", "#e23b3b"), ("#46101a", "#ff4d4d"), ("#2f0a10", "#d02f2f")],
    "PC":              [("#1a1030", "#7a4dd8"), ("#221338", "#9160ff"), ("#150d28", "#6b3fc4")],
}
DEFAULT_PALETTE = [("#1b1b22", "#6b6b7a")]

# Жанр задаёт фигуру. Ключ — подстрока из поля genre.
GENRE_SHAPES = {
    "Шутер": "inferno",
    "Приключенческий шутер": "space",
    "боевик": "mountains",
    "Приключения": "mountains",
    "Ролевая игра": "mist",
    "Ролевой боевик": "mountains",
    "гонки": "track",
    "Автосимулятор": "track",
    "Платформер": "islands",
    "Файтинг": "arena",
    "Выживание": "mist",
    "Рогалик": "arena",
    "Консоль": "device",
    "Геймпад": "device",
    "Гарнитура": "device",
    "Накопитель": "device",
    "Док-станция": "device",
}

# Отдельные товары, которым сцена по жанру не подходит: у киберпанка
# и космической ролевой игры обстановка важнее жанрового ярлыка.
SCENE_BY_SKU = {
    "MP-CP2077": "city",
    "XB-SF": "space",
    "NSW-MP4": "space",
    "PS5-GT7": "track",
    "MP-HADES2": "arena",
}


def shape_for(genre: str, sku: str = "") -> str:
    """Сцена для товара: сначала точечное исключение, потом жанр.

    Порядок именно такой: жанровое правило покрывает почти всё, но
    отдельные игры выбиваются, и переопределить одну строку проще,
    чем городить жанр «киберпанк» ради единственного товара.
    """
    if sku in SCENE_BY_SKU:
        return SCENE_BY_SKU[sku]
    # Сначала длинные ключи: «Приключенческий шутер» должен выиграть
    # у «Шутер», иначе более общее правило перехватит частное.
    for key in sorted(GENRE_SHAPES, key=len, reverse=True):
        if key.lower() in genre.lower():
            return GENRE_SHAPES[key]
    return "mountains"


def rng_stream(seed: str):
    """Детерминированный поток чисел 0..1 из артикула.

    Псевдослучайность здесь нужна ради разнообразия, но не должна
    менять картинку между запусками: обложка товара — часть его
    опознавания, и «каждый раз новая» хуже, чем просто другая.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    i = 0
    while True:
        if i >= len(digest):
            digest = hashlib.sha256(digest).digest()
            i = 0
        yield digest[i] / 255.0
        i += 1


def pick_palette(platform: str, seed: str) -> tuple[str, str]:
    options = PLATFORM_PALETTES.get(platform, DEFAULT_PALETTE)
    index = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(options)
    return options[index]


# ---------------------------------------------------------------- сцены
#
# Каждая сцена — это слои от дальнего к ближнему: небо, светило, дальний
# план, средний, ближний. Так строится любая постерная композиция,
# и так же она читается взглядом. Всё рисуется своей геометрией —
# ни кадров из игр, ни чужих иллюстраций здесь нет и быть не может:
# и то и другое защищено, независимо от того, коммерческий проект
# или учебный.


def sky(dark: str, accent: str) -> str:
    """Небо и светило — общий задник почти для всех сцен.

    Светило красится акцентом платформы, а не белым: белый круг
    на тёмном фоне выглядит серой кляксой, а цветной читается
    как источник света и связывает картинку с палитрой.
    """
    cx, cy = W * 0.70, H * 0.24
    return (
        f'<rect width="{W}" height="{H}" fill="url(#bg)"/>'
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="190" fill="{accent}" opacity="0.10"/>'
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="110" fill="{accent}" opacity="0.16"/>'
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="62" fill="{accent}" opacity="0.55"/>'
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="62" fill="none" stroke="#fff" '
        f'stroke-width="2" opacity="0.30"/>'
    )


def ridge(y: float, peaks: int, height: float, colour: str, opacity: float, r) -> str:
    """Горная гряда как ломаная линия.

    Одна функция на все планы: дальние гряды идут выше, светлее
    и ниже по амплитуде — так получается воздушная перспектива,
    самый дешёвый способ показать глубину.
    """
    points = [f"0,{H}"]
    step = W / peaks
    x = 0.0
    points.append(f"0,{y:.0f}")
    for _ in range(peaks + 1):
        peak_y = y - (0.35 + next(r) * 0.65) * height
        points.append(f"{x + step / 2:.0f},{peak_y:.0f}")
        points.append(f"{x + step:.0f},{y - next(r) * height * 0.2:.0f}")
        x += step
    points.append(f"{W},{H}")
    return f'<polygon points="{" ".join(points)}" fill="{colour}" opacity="{opacity:.2f}"/>'


def scene_mountains(r, dark: str, accent: str) -> str:
    """Горы, туман и сияние — приключения и открытый мир."""
    parts = [sky(dark, accent)]
    # Северное сияние: несколько дуг разной прозрачности.
    for i in range(4):
        y = H * (0.16 + i * 0.05)
        parts.append(
            f'<path d="M-40,{y:.0f} Q{W * 0.35:.0f},{y - 90 - next(r) * 60:.0f} '
            f'{W * 0.72:.0f},{y - 10:.0f} T{W + 40},{y - 40:.0f}" fill="none" '
            f'stroke="{accent}" stroke-width="{28 - i * 5}" opacity="{0.16 - i * 0.03:.2f}" '
            f'stroke-linecap="round"/>'
        )
    parts.append(ridge(H * 0.62, 4, 180, accent, 0.22, r))
    parts.append(ridge(H * 0.72, 3, 220, accent, 0.34, r))
    parts.append(ridge(H * 0.84, 3, 200, "#000", 0.42, r))
    parts.append(f'<rect y="{H * 0.86:.0f}" width="{W}" height="{H * 0.14:.0f}" '
                 f'fill="#000" opacity="0.30"/>')
    return "".join(parts)


def scene_city(r, dark: str, accent: str) -> str:
    """Силуэт города с неоном — киберпанк и современность."""
    parts = [sky(dark, accent)]
    base = H * 0.86
    x = -20.0
    layer = 0
    while x < W + 20:
        width = 40 + next(r) * 80
        height = 120 + next(r) * 420
        opacity = 0.30 + (layer % 3) * 0.16
        parts.append(
            f'<rect x="{x:.0f}" y="{base - height:.0f}" width="{width:.0f}" '
            f'height="{height:.0f}" fill="#000" opacity="{opacity:.2f}"/>'
        )
        # Окна: редкие светящиеся точки, иначе здание выглядит картонным.
        rows = int(height / 46)
        for row in range(rows):
            if next(r) > 0.34:
                continue
            wy = base - height + 18 + row * 46
            wx = x + 10 + next(r) * max(width - 24, 4)
            parts.append(f'<rect x="{wx:.0f}" y="{wy:.0f}" width="8" height="12" '
                         f'fill="{accent}" opacity="{0.4 + next(r) * 0.5:.2f}"/>')
        x += width + 6
        layer += 1
    parts.append(f'<rect y="{base:.0f}" width="{W}" height="{H - base:.0f}" '
                 f'fill="#000" opacity="0.45"/>')
    return "".join(parts)


def scene_space(r, dark: str, accent: str) -> str:
    """Планета с кольцом и звёздное поле — космос и научная фантастика."""
    parts = [f'<rect width="{W}" height="{H}" fill="url(#bg)"/>']
    for _ in range(90):
        x, y = next(r) * W, next(r) * H
        radius = 0.8 + next(r) * 2.0
        parts.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{radius:.1f}" '
                     f'fill="#fff" opacity="{0.25 + next(r) * 0.6:.2f}"/>')
    cx, cy, pr = W * 0.5, H * 0.46, 190
    parts.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{pr}" fill="{accent}" opacity="0.30"/>')
    parts.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{pr}" fill="none" '
                 f'stroke="{accent}" stroke-width="3" opacity="0.75"/>')
    # Терминатор — тень на половине планеты, иначе шар выглядит кругом.
    parts.append(f'<path d="M{cx - pr:.0f},{cy:.0f} a{pr},{pr} 0 0,0 {pr * 2},0 Z" '
                 f'fill="#000" opacity="0.35"/>')
    for i in range(3):
        parts.append(
            f'<ellipse cx="{cx:.0f}" cy="{cy + 16:.0f}" rx="{pr + 80 + i * 22}" '
            f'ry="{34 + i * 9}" fill="none" stroke="{accent}" stroke-width="{5 - i}" '
            f'opacity="{0.55 - i * 0.13:.2f}" transform="rotate(-14 {cx:.0f} {cy:.0f})"/>'
        )
    parts.append(ridge(H * 0.93, 3, 120, "#000", 0.55, r))
    return "".join(parts)


def scene_inferno(r, dark: str, accent: str) -> str:
    """Разломы, дым и зарево — шутеры и боевики."""
    parts = [f'<rect width="{W}" height="{H}" fill="url(#bg)"/>']
    parts.append(f'<circle cx="{W * 0.5:.0f}" cy="{H * 0.74:.0f}" r="300" '
                 f'fill="{accent}" opacity="0.22"/>')
    parts.append(f'<circle cx="{W * 0.5:.0f}" cy="{H * 0.78:.0f}" r="170" '
                 f'fill="{accent}" opacity="0.30"/>')
    # Дым: широкие мазки вверх.
    for i in range(5):
        x = 60 + i * 130 + next(r) * 60
        parts.append(
            f'<path d="M{x:.0f},{H * 0.8:.0f} Q{x - 70 + next(r) * 140:.0f},'
            f'{H * 0.45:.0f} {x - 40 + next(r) * 80:.0f},{H * 0.12:.0f}" fill="none" '
            f'stroke="#000" stroke-width="{50 + next(r) * 60:.0f}" opacity="0.16" '
            f'stroke-linecap="round"/>'
        )
    # Разломы в земле.
    base = H * 0.8
    for _ in range(6):
        x = next(r) * W
        parts.append(
            f'<path d="M{x:.0f},{H} L{x + 30 - next(r) * 60:.0f},{base + 40:.0f} '
            f'L{x + 10 - next(r) * 40:.0f},{base - next(r) * 90:.0f}" fill="none" '
            f'stroke="{accent}" stroke-width="{4 + next(r) * 10:.0f}" opacity="0.70"/>'
        )
    parts.append(f'<rect y="{base:.0f}" width="{W}" height="{H - base:.0f}" '
                 f'fill="#000" opacity="0.50"/>')
    parts.append(ridge(base + 10, 5, 90, "#000", 0.65, r))
    return "".join(parts)


def scene_track(r, dark: str, accent: str) -> str:
    """Трасса, уходящая к горизонту, — гонки."""
    parts = [sky(dark, accent)]
    horizon = H * 0.52
    parts.append(f'<rect y="{horizon:.0f}" width="{W}" height="{H - horizon:.0f}" '
                 f'fill="#000" opacity="0.42"/>')
    # Полотно дороги сходится в точку схода — простейшая перспектива.
    vx = W * 0.5
    parts.append(f'<polygon points="{vx - 26:.0f},{horizon:.0f} {vx + 26:.0f},{horizon:.0f} '
                 f'{W + 220},{H} {-220},{H}" fill="{accent}" opacity="0.16"/>')
    # Разметка: отрезки, которые удлиняются к зрителю.
    y = horizon + 12
    size = 6.0
    while y < H:
        parts.append(f'<rect x="{vx - size / 2:.0f}" y="{y:.0f}" width="{size:.0f}" '
                     f'height="{size * 2.2:.0f}" fill="#fff" opacity="0.55"/>')
        y += size * 4.4
        size *= 1.38
    for i in range(9):
        py = horizon + 20 + i * i * 8
        if py > H:
            break
        parts.append(f'<rect x="0" y="{py:.0f}" width="{W}" height="{2 + i}" '
                     f'fill="{accent}" opacity="{0.06 + i * 0.02:.2f}"/>')
    parts.append(ridge(horizon + 4, 4, 130, "#000", 0.45, r))
    return "".join(parts)


def scene_islands(r, dark: str, accent: str) -> str:
    """Парящие острова — платформеры и фэнтези."""
    parts = [sky(dark, accent)]
    for i in range(5):
        cx = 90 + next(r) * (W - 180)
        cy = 170 + i * 115 + next(r) * 50
        rx = 55 + next(r) * 85
        ry = rx * 0.24
        # Сначала подземная часть — клин вниз, он должен оказаться ПОД
        # верхней площадкой, иначе она окажется перечёркнутой.
        parts.append(
            f'<polygon points="{cx - rx:.0f},{cy:.0f} {cx + rx:.0f},{cy:.0f} '
            f'{cx + next(r) * 24 - 12:.0f},{cy + rx * 1.05:.0f}" '
            f'fill="#000" opacity="0.45"/>'
        )
        parts.append(
            f'<ellipse cx="{cx:.0f}" cy="{cy:.0f}" rx="{rx:.0f}" ry="{ry:.0f}" '
            f'fill="{accent}" opacity="0.75"/>'
        )
        parts.append(
            f'<ellipse cx="{cx - rx * 0.15:.0f}" cy="{cy - ry * 0.35:.0f}" '
            f'rx="{rx * 0.6:.0f}" ry="{ry * 0.5:.0f}" fill="#fff" opacity="0.18"/>'
        )
    parts.append(f'<rect y="{H * 0.88:.0f}" width="{W}" height="{H * 0.12:.0f}" '
                 f'fill="#000" opacity="0.32"/>')
    return "".join(parts)


def scene_arena(r, dark: str, accent: str) -> str:
    """Арена в свете прожекторов — файтинги."""
    parts = [f'<rect width="{W}" height="{H}" fill="url(#bg)"/>']
    for i in range(5):
        x = 60 + i * 130
        parts.append(
            f'<polygon points="{x:.0f},0 {x + 50:.0f},0 {x + 180:.0f},{H} {x - 130:.0f},{H}" '
            f'fill="{accent}" opacity="{0.06 + next(r) * 0.08:.2f}"/>'
        )
    cy = H * 0.72
    parts.append(f'<ellipse cx="{W / 2:.0f}" cy="{cy:.0f}" rx="260" ry="80" '
                 f'fill="{accent}" opacity="0.26"/>')
    parts.append(f'<ellipse cx="{W / 2:.0f}" cy="{cy:.0f}" rx="260" ry="80" fill="none" '
                 f'stroke="{accent}" stroke-width="5" opacity="0.75"/>')
    parts.append(f'<ellipse cx="{W / 2:.0f}" cy="{cy:.0f}" rx="150" ry="46" fill="none" '
                 f'stroke="{accent}" stroke-width="3" opacity="0.50"/>')
    parts.append(f'<rect y="{cy + 80:.0f}" width="{W}" height="{H - cy - 80:.0f}" '
                 f'fill="#000" opacity="0.42"/>')
    return "".join(parts)


def scene_mist(r, dark: str, accent: str) -> str:
    """Туман и мёртвый лес — выживание и ужасы."""
    parts = [f'<rect width="{W}" height="{H}" fill="url(#bg)"/>',
             '<defs><filter id="soft"><feGaussianBlur stdDeviation="26"/></filter></defs>']
    parts.append(f'<circle cx="{W * 0.5:.0f}" cy="{H * 0.3:.0f}" r="120" fill="#fff" '
                 f'opacity="0.10" filter="url(#soft)"/>')
    # Стволы: чем ближе, тем темнее и толще.
    for layer, (opacity, width_mul) in enumerate([(0.22, 0.6), (0.38, 0.9), (0.62, 1.4)]):
        for _ in range(7):
            x = next(r) * W
            top = H * (0.18 + next(r) * 0.2)
            thickness = (8 + next(r) * 14) * width_mul
            parts.append(f'<rect x="{x:.0f}" y="{top:.0f}" width="{thickness:.0f}" '
                         f'height="{H - top:.0f}" fill="#000" opacity="{opacity:.2f}"/>')
            for _branch in range(2):
                by = top + next(r) * 180
                dx = (40 + next(r) * 90) * (1 if next(r) > 0.5 else -1)
                parts.append(
                    f'<path d="M{x:.0f},{by:.0f} Q{x + dx / 2:.0f},{by - 40:.0f} '
                    f'{x + dx:.0f},{by - 70:.0f}" fill="none" stroke="#000" '
                    f'stroke-width="{thickness * 0.4:.0f}" opacity="{opacity:.2f}"/>'
                )
        parts.append(f'<rect y="{H * (0.45 + layer * 0.12):.0f}" width="{W}" '
                     f'height="200" fill="#fff" opacity="0.07" filter="url(#soft)"/>')
    return "".join(parts)


def scene_device(r, dark: str, accent: str) -> str:
    """Подсвеченная витрина — консоли и аксессуары.

    Намеренно НЕ силуэт приставки или геймпада: узнаваемая форма
    устройства — это чужой промышленный дизайн. Здесь подиум, свет
    и сетка, то есть обстановка вокруг товара, а не сам товар.
    """
    parts = [f'<rect width="{W}" height="{H}" fill="url(#bg)"/>']
    for i in range(0, W, 48):
        parts.append(f'<line x1="{i}" y1="0" x2="{i}" y2="{H}" stroke="{accent}" '
                     f'stroke-width="1" opacity="0.12"/>')
    for i in range(0, H, 48):
        parts.append(f'<line x1="0" y1="{i}" x2="{W}" y2="{i}" stroke="{accent}" '
                     f'stroke-width="1" opacity="0.12"/>')
    # Конус света сверху.
    parts.append(f'<polygon points="{W * 0.34:.0f},0 {W * 0.66:.0f},0 '
                 f'{W * 0.88:.0f},{H * 0.78:.0f} {W * 0.12:.0f},{H * 0.78:.0f}" '
                 f'fill="#fff" opacity="0.07"/>')
    cy = H * 0.72
    parts.append(f'<ellipse cx="{W / 2:.0f}" cy="{cy:.0f}" rx="210" ry="56" '
                 f'fill="{accent}" opacity="0.30"/>')
    parts.append(f'<ellipse cx="{W / 2:.0f}" cy="{cy:.0f}" rx="210" ry="56" fill="none" '
                 f'stroke="{accent}" stroke-width="4" opacity="0.80"/>')
    parts.append(f'<rect x="{W / 2 - 210:.0f}" y="{cy:.0f}" width="420" height="40" '
                 f'fill="{accent}" opacity="0.18"/>')
    parts.append(f'<ellipse cx="{W / 2:.0f}" cy="{cy + 40:.0f}" rx="210" ry="56" '
                 f'fill="#000" opacity="0.30"/>')
    return "".join(parts)


DRAWERS = {
    "mountains": scene_mountains, "city": scene_city, "space": scene_space,
    "inferno": scene_inferno, "track": scene_track, "islands": scene_islands,
    "arena": scene_arena, "mist": scene_mist, "device": scene_device,
}


def render(product: dict) -> str:
    sku = product["sku"]
    dark, accent = pick_palette(product.get("platform", ""), sku)
    shape = shape_for(product.get("genre", ""), sku)
    r = rng_stream(sku)

    figure = DRAWERS[shape](r, dark, accent)
    emoji = product.get("emoji", "📦")

    # Виньетка поверх фигур: она притемняет низ обложки, и значок жанра,
    # который витрина кладёт поверх картинки, читается на любом рисунке.
    # Градиент внутри SVG дешевле полупрозрачного слоя в вёрстке
    # и уезжает вместе с картинкой, куда бы её ни вставили.
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img"
     aria-label="Обложка: {escape(product["title"])}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{dark}"/>
      <stop offset="55%" stop-color="{dark}"/>
      <stop offset="100%" stop-color="{accent}" stop-opacity="0.40"/>
    </linearGradient>
    <linearGradient id="vignette" x1="0" y1="0" x2="0" y2="1">
      <stop offset="55%" stop-color="#000" stop-opacity="0"/>
      <stop offset="100%" stop-color="#000" stop-opacity="0.65"/>
    </linearGradient>
  </defs>
  {figure}
  <rect width="{W}" height="{H}" fill="url(#vignette)"/>
  <text x="{W / 2}" y="{H / 2}" font-size="150" text-anchor="middle"
        dominant-baseline="central" opacity="0.95">{emoji}</text>
</svg>
'''


def escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def main() -> int:
    parser = argparse.ArgumentParser(description="обложки товаров")
    parser.add_argument("--check", action="store_true",
                        help="только проверить, что файл есть у каждого товара")
    args = parser.parse_args()

    products = json.loads(CATALOG.read_text(encoding="utf-8"))["products"]

    if args.check:
        missing = [p["sku"] for p in products if not (ART_DIR / f"{p['sku']}.svg").exists()]
        if missing:
            print(f"нет обложек: {', '.join(missing)}")
            return 1
        print(f"обложки на месте: {len(products)}")
        return 0

    ART_DIR.mkdir(parents=True, exist_ok=True)
    for product in products:
        (ART_DIR / f"{product['sku']}.svg").write_text(render(product), encoding="utf-8")
    print(f"нарисовано обложек: {len(products)} -> {ART_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
