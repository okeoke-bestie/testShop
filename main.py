#!/usr/bin/env python3
"""ГЛАВНЫЙ ФАЙЛ ПРОЕКТА. Запускать нужно именно его.

    python main.py            меню
    python main.py race       гонка за последним товаром: было и стало
    python main.py nplus1     проблема N+1 в числах
    python main.py pages      keyset-пагинация против OFFSET
    python main.py order      сценарий заказа целиком
    python main.py shop       магазин: витрина и API
    python main.py migrate    миграции схемы PostgreSQL
    python main.py test       прогнать тесты

Каждая команда печатает не только результат, но и объяснение,
что именно она показывает.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

# Версия печатается в меню и в баннере магазина. Нужна не для красоты:
# когда «у меня не работает», первый вопрос — та ли это вообще сборка.
# Без видимой версии выясняется это по косвенным признакам.
VERSION = "6.0.0"

BOLD, DIM, GREEN, YELLOW, RED, OFF = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)


def header(text: str) -> None:
    print(f"\n{BOLD}{'=' * 74}{OFF}\n{BOLD}{text}{OFF}\n{BOLD}{'=' * 74}{OFF}")


def hint(text: str) -> None:
    print(f"{DIM}{text}{OFF}")


# Порт витрины. 8080, а не 8000: восьмитысячный занимают почти все
# учебные проекты сразу, и «адрес уже используется» — самая частая
# ошибка при запуске. Меняется двумя способами: SHOP_PORT в .env
# или --port при запуске; флаг важнее переменной.
DEFAULT_PORT = 8080

DB_FILE = ROOT / "shop.sqlite3"
CATALOG_FILE = ROOT / "data" / "catalog.json"
REVIEWS_FILE = ROOT / "data" / "reviews.json"
CUSTOMERS_FILE = ROOT / "data" / "customers.json"
ENV_FILE = ROOT / ".env"


def load_env() -> None:
    """Подкладывает .env, если он есть. Окружение важнее файла."""
    from shopapi.env_file import load

    load()


def resolve_port(explicit: int | None) -> int:
    """Порт: флаг -> SHOP_PORT -> PORT -> значение по умолчанию.

    Тот же порядок, что и у строки подключения: явно переданное важнее
    настройки, настройка важнее умолчания. Мусор в переменной не должен
    ронять запуск — о нём сообщается и берётся умолчание.

    `PORT` без префикса читается ради хостингов: почти все они не дают
    выбрать порт, а сообщают его этой переменной и ждут, что сервис
    послушается. Свой `SHOP_PORT` при этом главнее — иначе чужая
    переменная в окружении молча переопределяла бы настройку проекта.
    """
    if explicit:
        return explicit
    for name in ("SHOP_PORT", "PORT"):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError:
            print(f"{YELLOW}{name}={raw!r} — не число, беру {DEFAULT_PORT}{OFF}")
            return DEFAULT_PORT
        if not 1 <= port <= 65535:
            print(f"{YELLOW}{name}={port} вне диапазона, беру {DEFAULT_PORT}{OFF}")
            return DEFAULT_PORT
        return port
    return DEFAULT_PORT


def port_is_free(port: int) -> bool:
    """Проверка ДО запуска — чтобы не получить трассировку на пол-экрана.

    uvicorn на занятом порту падает с `[Errno 98] Address already in use`
    и стеком вызовов, из которого человеку ничего не следует. Понятная
    строка с готовой командой полезнее.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def build(db_path=None, dsn=None):
    """Собирает бэкенд через фабрику.

    Выбор базы: явный DSN -> переменная DATABASE_URL -> sqlite.
    Сервисы и демонстрации одинаковы для обеих баз — в этом и смысл
    разделения слоёв.
    """
    from shopapi.factory import make_backend

    backend = make_backend(dsn=dsn, sqlite_path=str(db_path) if db_path else None)
    return (
        backend.db,
        backend.products,
        backend.orders,
        backend.order_service,
        backend.catalog_service,
    )


# ---------------------------------------------------------------- команды

def cmd_race(args) -> int:
    """Гонка за последним товаром: наивная версия против атомарной."""
    header("ГОНКА ЗА ПОСЛЕДНИМ ТОВАРОМ")
    hint("На складе ОДИН экземпляр. Двадцать покупателей жмут «Купить»")
    hint("одновременно. Сколько раз товар будет продан?\n")

    threads = 20

    for label, method_name, comment in [
        ("НАИВНО", "reserve_stock_unsafe", "прочитали остаток -> проверили -> записали"),
        ("АТОМАРНО", "reserve_stock", "UPDATE ... WHERE stock >= ? одной командой"),
    ]:
        db, products, _, _, _ = build()
        product = products.add("SKU-LAST", "Последний экземпляр", 499900, 1)
        method = getattr(products, method_name)

        barrier = threading.Barrier(threads)
        wins = []
        lock = threading.Lock()

        # Значения захватываются аргументами по умолчанию, а не замыканием.
        # Замыкание в Python связывает ПЕРЕМЕННУЮ, а не значение: к моменту
        # вызова цикл уже уйдёт на следующую итерацию, и функция увидит
        # чужой товар. Классическая ловушка, её ловит линтер (B023).
        def buy(_=None, method=method, barrier=barrier, wins=wins,
                lock=lock, product_id=product.id):
            barrier.wait()  # выстраиваем потоки на одном старте
            if method(product_id, 1):
                with lock:
                    wins.append(1)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(buy, range(threads)))

        stock = products.get(product.id).stock
        colour = RED if len(wins) != 1 else GREEN
        print(f"{BOLD}{label}{OFF}  {DIM}({comment}){OFF}")
        print(f"  продано экземпляров: {colour}{len(wins)}{OFF}   остаток: {stock}")
        if len(wins) > 1:
            print(f"  {RED}ОВЕРСЕЛЛИНГ: продали {len(wins)} шт., а был 1{OFF}")
        elif label == "НАИВНО":
            # Гонка вероятностная: иногда потоки расходятся по времени
            # и она не срабатывает. Честно сказать об этом лучше, чем
            # сделать вид, что наивная версия исправна.
            print(f"  {YELLOW}в этот раз гонка не сработала — запусти ещё раз,"
                  f" она вероятностная{OFF}")
        else:
            print(f"  {GREEN}корректно: один товар — один покупатель{OFF}")
        print()
        db.close()

    hint("Почему так: между чтением остатка и записью успевают вклиниться")
    hint("другие потоки. Атомарный UPDATE проверяет условие в момент записи,")
    hint("под блокировкой строки — вклиниться некуда.\n")
    hint("Отдельная находка проекта: определять успех по cursor.rowcount")
    hint("нельзя — при общем соединении он возвращает 0 всем потокам,")
    hint("включая победителя. Поэтому используется UPDATE ... RETURNING.")
    return 0


def cmd_nplus1(args) -> int:
    """Проблема N+1 в числах."""
    header("ПРОБЛЕМА N+1")
    hint("Загружаем 30 заказов вместе со строками. Двумя способами.\n")

    db, products, orders_repo, service, _ = build()
    from shopapi.services.orders import OrderRequest

    product = products.add("SKU-DEMO", "Товар", 100000, 1000)
    for _ in range(30):
        service.create_order(OrderRequest(customer_id=1, items=[(product.id, 1)]))

    for label, method_name, comment in [
        ("НАИВНО", "list_for_customer_nplus1", "цикл с запросом внутри"),
        ("ПАЧКОЙ", "list_for_customer", "один запрос с IN + раскладка в памяти"),
    ]:
        method = getattr(orders_repo, method_name)
        with db.counter.measure():
            started = time.perf_counter()
            result = method(customer_id=1)
            elapsed = (time.perf_counter() - started) * 1000
        colour = RED if db.counter.count > 5 else GREEN
        print(f"{BOLD}{label}{OFF}  {DIM}({comment}){OFF}")
        print(f"  заказов: {len(result)}   запросов к базе: {colour}{db.counter.count}{OFF}"
              f"   время: {elapsed:.1f} мс")
        print()

    hint("На локальной базе разница во времени невелика. В проде каждый")
    hint("запрос — это поход по сети: 31 запрос по 2 мс это 62 мс вместо 4.")
    hint("И растёт линейно с числом заказов, а второй вариант — нет.")
    db.close()
    return 0


def cmd_pages(args) -> int:
    """Keyset-пагинация."""
    header("ПАГИНАЦИЯ ПО КУРСОРУ")
    hint("25 товаров, листаем по 10.\n")

    db, products, _, _, _ = build()
    for i in range(25):
        products.add(f"SKU-{i:03d}", f"Товар {i:03d}", 1000 + i * 10, 5)

    cursor = None
    page_no = 0
    while True:
        page = products.list_page(limit=10, cursor=cursor)
        page_no += 1
        titles = ", ".join(p.title.split()[-1] for p in page.items)
        print(f"  страница {page_no}: {len(page.items)} шт.  [{titles}]")
        if not page.has_more:
            break
        cursor = page.next_cursor
        print(f"    {DIM}курсор: {cursor[:40]}...{OFF}")

    print()
    hint("Почему не OFFSET: LIMIT 20 OFFSET 10000 заставляет базу прочитать")
    hint("и выбросить 10000 строк — чем глубже страница, тем медленнее.")
    hint("И при вставке новой записи выдача сдвигается: одна строка")
    hint("покажется дважды, другая пропадёт. Курсор от этого свободен.")
    db.close()
    return 0


def cmd_order(args) -> int:
    """Полный сценарий заказа."""
    header("СЦЕНАРИЙ ЗАКАЗА")

    from shopapi.errors import IdempotencyConflict, OutOfStock
    from shopapi.services.orders import OrderRequest

    db_path = Path(args.db) if getattr(args, "db", None) else None
    db, products, _, service, _ = build(db_path)

    if db_path:
        # Работаем с постоянной базой — берём первые два товара оттуда.
        rows = products.db.query_all("SELECT id FROM products ORDER BY sku LIMIT 2")
        if len(rows) < 2:
            print(f"{YELLOW}В базе меньше двух товаров.{OFF} "
                  f"Сначала: python main.py seed")
            db.close()
            return 1
        console = products.get(rows[0]["id"])
        game = products.get(rows[1]["id"])
    else:
        console = products.add("SKU-001", "Консоль игровая", 4999000, 3)
        game = products.add("SKU-002", "Игра приключенческая", 349900, 10)

    print(f"{BOLD}Каталог:{OFF}")
    for p in (console, game):
        print(f"  {p.sku}  {p.title:<26} {p.price_rub:>10} ₽   остаток {p.stock}")

    print(f"\n{BOLD}1. Обычный заказ{OFF}")
    order = service.create_order(
        OrderRequest(customer_id=1, items=[(console.id, 1), (game.id, 2)])
    )
    print(f"  {GREEN}заказ #{order.id}{OFF}, сумма {order.total_kopecks / 100:.2f} ₽")
    print(f"  остаток консолей: {products.get(console.id).stock}")

    print(f"\n{BOLD}2. Повтор с тем же ключом идемпотентности{OFF}")
    request = OrderRequest(
        customer_id=1, items=[(game.id, 1)], idempotency_key="abc-123"
    )
    first = service.create_order(request)
    second = service.create_order(request)
    print(f"  первый вызов:  заказ #{first.id}")
    print(f"  второй вызов:  заказ #{second.id}  "
          f"{GREEN}(тот же, дубля нет){OFF}")
    print(f"  остаток игр: {products.get(game.id).stock} {DIM}(списано один раз){OFF}")

    print(f"\n{BOLD}3. Тот же ключ, но другой заказ{OFF}")
    try:
        service.create_order(
            OrderRequest(customer_id=1, items=[(console.id, 1)], idempotency_key="abc-123")
        )
    except IdempotencyConflict as exc:
        print(f"  {YELLOW}{exc.code}{OFF}: {exc.message}")
        print(f"  {DIM}молча вернуть старый заказ нельзя — клиент просил другое{OFF}")

    print(f"\n{BOLD}4. Не хватает товара{OFF}")
    try:
        service.create_order(OrderRequest(customer_id=2, items=[(console.id, 99)]))
    except OutOfStock as exc:
        print(f"  {YELLOW}{exc.code}{OFF} (HTTP {exc.http_status}): {exc.message}")
        print(f"  {DIM}запрошено {exc.details['requested']}, "
              f"доступно {exc.details['available']}{OFF}")

    print(f"\n{BOLD}5. Отмена заказа{OFF}")
    before = products.get(console.id).stock
    service.cancel_order(order.id)
    after = products.get(console.id).stock
    print(f"  остаток консолей: {before} -> {after} {GREEN}(товар вернулся){OFF}")

    db.close()
    return 0


def cmd_seed(args) -> int:
    """Создаёт базу и заливает в неё каталог из файла."""
    header("ЗАГРУЗКА КАТАЛОГА В БАЗУ")

    from shopapi.catalog_file import load_catalog, seed_database
    from shopapi.errors import ValidationError

    catalog_path = Path(args.catalog)
    db_path = Path(args.db)

    use_pg = bool(os.environ.get("DATABASE_URL"))
    print(f"Каталог: {catalog_path}")
    print(f"База:    {'PostgreSQL (из DATABASE_URL)' if use_pg else db_path}\n")

    try:
        items = load_catalog(catalog_path)
    except ValidationError as exc:
        print(f"{RED}Каталог не прочитан.{OFF}\n{exc.message}")
        return 1

    if not use_pg and db_path.exists() and args.fresh:
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            extra = Path(str(db_path) + suffix)
            if extra.exists():
                extra.unlink()
        print(f"{DIM}Старая база удалена (--fresh){OFF}")

    from shopapi.factory import make_backend

    backend = make_backend(sqlite_path=None if use_pg else str(db_path))
    db, products = backend.db, backend.products
    try:
        if args.fresh and use_pg:
            # В PostgreSQL «с нуля» — это чистка таблиц, а не удаление
            # файла: база живёт на сервере, а не рядом с проектом.
            # Порядок важен: сначала то, что ссылается, потом то,
            # на что ссылаются, иначе внешний ключ не даст удалить.
            db.execute("DELETE FROM reviews")
            db.execute("DELETE FROM customers")
            db.execute("DELETE FROM order_lines")
            db.execute("DELETE FROM orders")
            db.execute("DELETE FROM products")
            print(f"{DIM}Старые данные удалены (--fresh){OFF}")
        count = seed_database(products, items)
    except ValidationError as exc:
        print(f"{RED}{exc.message}{OFF}")
        print(f"{DIM}Подсказка: python main.py seed --fresh{OFF}")
        db.close()
        return 1

    print(f"{GREEN}Загружено товаров: {count}{OFF}")

    # Отзывы заливаются той же командой: каталог и отзывы — это один
    # набор демонстрационных данных, и держать их в разных командах
    # значит гарантированно однажды залить только половину.
    from shopapi.reviews_file import (
        load_customers,
        load_reviews,
        seed_customers,
        seed_reviews,
    )
    try:
        people = load_customers(CUSTOMERS_FILE)
        reviews = load_reviews(REVIEWS_FILE)
    except ValidationError as exc:
        print(f"{YELLOW}Отзывы не прочитаны: {exc.message}{OFF}")
    else:
        # Порядок важен: отзыв ссылается на покупателя, поэтому профили
        # заливаются первыми. Иначе связь просто не установится,
        # и «покупка подтверждена» не покажется ни у одного отзыва.
        added_people = seed_customers(backend.customers, people)
        added = seed_reviews(backend.reviews, products, reviews, backend.customers)
        print(f"{GREEN}Загружено покупателей: {added_people}{OFF}  "
              f"{DIM}(демонстрационные){OFF}")
        print(f"{GREEN}Загружено отзывов: {added}{OFF}  "
              f"{DIM}(демонстрационные, помечены в базе){OFF}")
    print()

    _print_catalog(products)
    db.close()

    where = "PostgreSQL" if use_pg else f"файл {db_path}"
    print(f"\n{DIM}Данные записаны в: {where}{OFF}")
    print(f"{DIM}Правь data/catalog.json и запускай seed снова.{OFF}")
    return 0


def cmd_catalog(args) -> int:
    """Показывает, что сейчас лежит в базе."""
    header("КАТАЛОГ В БАЗЕ")

    use_pg = bool(os.environ.get("DATABASE_URL"))
    db_path = Path(args.db)
    if not use_pg and not db_path.exists():
        print(f"{YELLOW}Базы ещё нет.{OFF} Создай её: python main.py seed")
        return 1

    db, products, _, _, _ = build(None if use_pg else db_path)
    print(f"{DIM}база: {'PostgreSQL' if use_pg else db_path}{OFF}\n")
    _print_catalog(products)
    db.close()
    return 0


def cmd_stock(args) -> int:
    """Меняет остаток товара прямо в базе."""
    header("ИЗМЕНЕНИЕ ОСТАТКА")

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"{YELLOW}Базы ещё нет.{OFF} Создай её: python main.py seed")
        return 1

    db, products, _, _, _ = build(db_path)
    row = db.query_one("SELECT * FROM products WHERE sku = ?", (args.sku,))
    if row is None:
        print(f"{RED}Товар с артикулом {args.sku} не найден.{OFF}")
        print(f"{DIM}Список: python main.py catalog{OFF}")
        db.close()
        return 1

    before = row["stock"]
    db.execute(
        "UPDATE products SET stock = ?, version = version + 1 WHERE sku = ?",
        (args.quantity, args.sku),
    )
    print(f"  {row['title']} ({args.sku})")
    print(f"  остаток: {before} -> {GREEN}{args.quantity}{OFF}")
    db.close()

    print(f"\n{DIM}Это правка в базе. Чтобы изменение пережило пересоздание{OFF}")
    print(f"{DIM}базы, поправь ещё и data/catalog.json.{OFF}")
    return 0


def _print_catalog(products) -> None:
    rows = products.db.query_all("SELECT * FROM products ORDER BY sku")
    if not rows:
        print(f"{YELLOW}Каталог пуст.{OFF}")
        return
    print(f"  {'АРТИКУЛ':<10} {'НАЗВАНИЕ':<28} {'ЦЕНА':>12} {'ОСТАТОК':>9}")
    print(f"  {'-' * 10} {'-' * 28} {'-' * 12} {'-' * 9}")
    for row in rows:
        price = f"{row['price_kopecks'] // 100}.{row['price_kopecks'] % 100:02d}"
        stock = row["stock"]
        colour = RED if stock == 0 else (YELLOW if stock < 3 else "")
        print(f"  {row['sku']:<10} {row['title']:<28} {price:>12} "
              f"{colour}{stock:>9}{OFF}")


def cmd_shop(args) -> int:
    """Поднимает магазин: API и витрину."""
    header(f"МАГАЗИН GAMEPORT   v{VERSION}")

    port = resolve_port(args.port)
    if not port_is_free(port):
        print(f"{RED}Порт {port} занят.{OFF}")
        print(f"{DIM}Скорее всего, магазин уже запущен в другом окне.{OFF}\n")
        print("Запустить на другом порту:")
        print(f"    python main.py shop --port {port + 1}")
        print("\nИли задать порт по умолчанию в .env:")
        print(f"    SHOP_PORT={port + 1}")
        return 1

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    env = dict(os.environ)

    if dsn:
        hint(f"База: PostgreSQL  {_mask(dsn)}")
        hint("Данные переживают перезапуск.")
        if args.dsn:
            env["DATABASE_URL"] = args.dsn
    else:
        # Без Postgres витрина всё равно должна работать «по-настоящему»:
        # магазин, который забывает заказы при перезапуске, ничего
        # не демонстрирует. Поэтому здесь постоянный файл, а не temp —
        # временная база остаётся только у демонстраций, которым нужны
        # свои условия и чистый старт.
        env["SHOP_SQLITE_PATH"] = str(DB_FILE)
        if not DB_FILE.exists():
            print(f"{DIM}Базы ещё нет — создаю и заливаю каталог...{OFF}")
            code = run(["seed"])
            if code != 0:
                return code
            header(f"МАГАЗИН GAMEPORT   v{VERSION}")
        hint(f"База: sqlite, файл {DB_FILE.name} — данные сохраняются.")
        hint("Для PostgreSQL:  python main.py pg")

    print()
    print(f"  {BOLD}Магазин:{OFF}      {GREEN}http://localhost:{port}{OFF}")
    hint(f"  Документация: http://localhost:{port}/docs")
    print()

    return subprocess.call(
        [sys.executable, "-m", "uvicorn", "shopapi.api:app",
         "--port", str(port), "--app-dir", "src"],
        cwd=str(ROOT), env=env,
    )


def cmd_migrate(args) -> int:
    """Применяет миграции схемы к PostgreSQL."""
    header("МИГРАЦИИ СХЕМЫ")

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        print(f"{YELLOW}Миграции нужны только для PostgreSQL.{OFF}")
        print(f"{DIM}Задай DATABASE_URL или передай --dsn{OFF}")
        return 1

    from shopapi.migrate import MIGRATIONS_TABLE, migrate, status  # noqa: F401
    from shopapi.pg import PostgresDatabase

    db = PostgresDatabase(dsn)
    try:
        if args.status:
            for migration, applied in status(db, ROOT / "migrations"):
                mark = f"{GREEN}применена{OFF}" if applied else f"{YELLOW}ожидает{OFF}"
                print(f"  {migration.version:03d}_{migration.name:<24} {mark}")
            return 0

        applied = migrate(db, ROOT / "migrations")
        if not applied:
            print(f"{GREEN}Схема актуальна{OFF}, применять нечего.")
        for migration in applied:
            print(f"  {GREEN}применена{OFF} {migration.version:03d}_{migration.name}")
        return 0
    finally:
        db.close()


DEFAULT_DSN = "postgresql://shop:shop@localhost:5432/shop"


def cmd_pg(args) -> int:
    """Проверяет готовность к PostgreSQL и объясняет, чего не хватает.

    Команда диагностическая: она не «чинит молча», а по шагам говорит,
    что сейчас есть и что сделать дальше. Это осознанно — настройка
    подключения к базе должна быть понятной, а не магической.
    """
    header("ПОДКЛЮЧЕНИЕ К POSTGRESQL")

    from shopapi.env_file import source_of

    ok = True

    # --- шаг 1: драйвер -------------------------------------------------
    print(f"{BOLD}1. Драйвер psycopg{OFF}")
    try:
        import psycopg  # noqa: F401
        from psycopg_pool import ConnectionPool  # noqa: F401
    except ImportError:
        ok = False
        print(f"   {RED}не установлен{OFF}")
        print(f'   {DIM}pip install -e ".[serve,postgres]"{OFF}')
    else:
        print(f"   {GREEN}установлен{OFF}")

    # --- шаг 2: строка подключения --------------------------------------
    dsn = args.dsn or os.environ.get("DATABASE_URL")
    print(f"\n{BOLD}2. DATABASE_URL{OFF}")
    if dsn:
        print(f"   {GREEN}задан{OFF}  {DIM}(источник: {source_of('DATABASE_URL')}){OFF}")
        print(f"   {DIM}{_mask(dsn)}{OFF}")
    else:
        ok = False
        print(f"   {RED}не задан{OFF} — поэтому проект работает на sqlite")
        if args.write_env:
            # Файл мог существовать и хранить чужие ключи — дописываем,
            # а не перезаписываем. Затереть чужие настройки «для удобства»
            # хуже, чем не сработать.
            existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
            if existing and not existing.endswith("\n"):
                existing += "\n"
            ENV_FILE.write_text(
                existing + f"DATABASE_URL={DEFAULT_DSN}\n", encoding="utf-8"
            )
            dsn = DEFAULT_DSN
            os.environ["DATABASE_URL"] = dsn
            action = "дописал строку в" if existing else "создал файл"
            print(f"   {GREEN}{action} .env{OFF}  {DIM}{DEFAULT_DSN}{OFF}")
        else:
            print(f"   {DIM}самый простой способ — положить строку в файл .env:{OFF}")
            print(f"   {DIM}python main.py pg --write-env{OFF}")

    # --- шаг 3: сама база ------------------------------------------------
    print(f"\n{BOLD}3. Сервер базы{OFF}")
    if not dsn:
        print(f"   {DIM}пропущено: нечем подключаться{OFF}")
    else:
        try:
            import psycopg
        except ImportError:
            print(f"   {DIM}пропущено: нет драйвера{OFF}")
        else:
            # Подключаемся напрямую, а не через пул: пул на недоступной
            # базе делает несколько попыток с паузами и пишет свои
            # предупреждения — для диагностики нужен один короткий
            # ответ «да/нет» и текст ошибки.
            try:
                with psycopg.connect(dsn, connect_timeout=3) as conn:
                    version = conn.execute("SELECT version()").fetchone()[0]
            except Exception as exc:  # noqa: BLE001
                ok = False
                print(f"   {RED}не отвечает{OFF}")
                print(f"   {DIM}{type(exc).__name__}: {str(exc).strip().splitlines()[0]}{OFF}")
                print(f"\n   {DIM}Поднять базу в докере:{OFF}")
                print("     docker compose up -d db")
            else:
                print(f"   {GREEN}отвечает{OFF}")
                print(f"   {DIM}{version.split(',')[0]}{OFF}")

    # --- итог -------------------------------------------------------------
    print()
    if ok and dsn:
        hint("Всё на месте. Дальше:")
        print("   python main.py migrate     # схема")
        print("   python main.py seed        # каталог")
        print("   python main.py shop        # магазин\n")
        hint("Проверить, что база настоящая: купи товар, останови сервер,")
        hint("запусти снова — остаток и заказ останутся на месте.")
        return 0

    print(f"{YELLOW}Пока не всё готово.{OFF} Порядок с нуля:\n")
    print('   pip install -e ".[serve,postgres]"')
    print("   docker compose up -d db")
    print("   python main.py pg --write-env")
    print("   python main.py migrate")
    print("   python main.py seed")
    print("   python main.py shop\n")
    hint("Без PostgreSQL проект тоже работает — на sqlite во временном")
    hint("файле. Разница одна: данные не переживают перезапуск.")
    return 1


def _mask(dsn: str) -> str:
    """Прячет пароль: строка подключения попадает в логи и в скриншоты."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    creds, _, host = rest.rpartition("@")
    if ":" in creds:
        user, _, _password = creds.partition(":")
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


def cmd_test(args) -> int:
    header("ТЕСТЫ")
    hint("76 тестов. Главные — на конкурентность: они запускают")
    hint("настоящие потоки и проверяют, что товар нельзя продать дважды.\n")
    return subprocess.call([sys.executable, "-m", "pytest", "-q"], cwd=str(ROOT))


def cmd_serve(args) -> int:
    header("HTTP-СЕРВИС")
    port = resolve_port(None)
    hint(f"Документация API: http://localhost:{port}/docs")
    hint("Нужны зависимости: pip install -e \".[serve]\"\n")
    return subprocess.call(
        [sys.executable, "-m", "uvicorn", "shopapi.api:app",
         "--port", str(port), "--app-dir", "src"],
        cwd=str(ROOT),
    )


MENU = [
    ("shop", "Открыть магазин", "витрина + API, начни с этого"),
    ("race", "Гонка за последним товаром", "почему товар не продаётся дважды"),
    ("order", "Сценарий заказа в консоли", "идемпотентность, отмена, ошибки"),
    ("pg", "Подключить PostgreSQL", "проверка и пошаговая настройка"),
    ("seed", "Загрузить каталог в базу", "данные из data/catalog.json"),
    ("catalog", "Показать товары в базе", "что сейчас на складе"),
    ("migrate", "Применить миграции", "только для PostgreSQL"),
    ("nplus1", "Проблема N+1 в числах", "31 запрос против 2"),
    ("pages", "Пагинация по курсору", "почему не OFFSET"),
    ("test", "Прогнать тесты", "доли секунды"),
]


def cmd_menu(args) -> int:
    print(f"""
{BOLD}shopapi — сервис заказов небольшого магазина{OFF}  {DIM}v{VERSION}{OFF}

Показывает, как решаются задачи, из которых состоит бэкенд:
параллельные заказы на один товар, повторные запросы, работа
с базой без лишних запросов, устойчивость к отказам.

{BOLD}Товары лежат в data/catalog.json — этот файл можно править.{OFF}
После правки: пункт 3 (создать базу), затем пункт 4 (посмотреть).

{BOLD}Что запустить:{OFF}
""")
    for i, (_name, title, note) in enumerate(MENU, start=1):
        print(f"  {GREEN}{i:>2}{OFF}  {title:<30} {DIM}{note}{OFF}")
    print(f"  {GREEN}{0:>2}{OFF}  Выход\n")

    try:
        choice = input(f"{BOLD}Введи номер и нажми Enter: {OFF}").strip()
    except (EOFError, KeyboardInterrupt, OSError):
        print(f"""
{DIM}Ввод недоступен.{OFF} Запусти команду напрямую:

    python main.py race
    python main.py order

{BOLD}В PyCharm:{OFF} терминал внизу окна (Alt+F12) — там работают
обычные команды. Либо Run -> Edit Configurations -> Parameters -> race.
""")
        return 0

    if choice in {"", "0", "выход", "q"}:
        return 0
    if not choice.isdigit() or not (1 <= int(choice) <= len(MENU)):
        print(f"{YELLOW}Нет такого пункта: {choice!r}{OFF}")
        return 1

    command = MENU[int(choice) - 1][0]
    print(f"{DIM}Запускаю: python main.py {command}{OFF}")
    return run([command])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="main.py", description="shopapi")
    sub = parser.add_subparsers(dest="command")

    for name, func in [
        ("race", cmd_race), ("nplus1", cmd_nplus1), ("pages", cmd_pages),
        ("test", cmd_test), ("serve", cmd_serve),
    ]:
        p = sub.add_parser(name)
        p.set_defaults(func=func)

    p = sub.add_parser("order", help="сценарий заказа")
    p.add_argument("--db", default=None, help="работать с постоянной базой")
    p.set_defaults(func=cmd_order)

    p = sub.add_parser("seed", help="создать базу и залить каталог")
    p.add_argument("--catalog", default=str(CATALOG_FILE))
    p.add_argument("--db", default=str(DB_FILE))
    p.add_argument("--fresh", action="store_true", help="удалить старую базу")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("catalog", help="показать товары в базе")
    p.add_argument("--db", default=str(DB_FILE))
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("shop", help="поднять магазин с витриной")
    p.add_argument("--port", type=int, default=None,
                   help=f"порт витрины (по умолчанию {DEFAULT_PORT} или SHOP_PORT)")
    p.add_argument("--dsn", default=None, help="строка подключения к PostgreSQL")
    p.set_defaults(func=cmd_shop)

    p = sub.add_parser("migrate", help="применить миграции PostgreSQL")
    p.add_argument("--dsn", default=None)
    p.add_argument("--status", action="store_true", help="только показать состояние")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("pg", help="проверить и настроить PostgreSQL")
    p.add_argument("--dsn", default=None)
    p.add_argument("--write-env", action="store_true",
                   help="создать .env со строкой подключения по умолчанию")
    p.set_defaults(func=cmd_pg)

    p = sub.add_parser("stock", help="изменить остаток товара")
    p.add_argument("sku", help="артикул, например GAME-003")
    p.add_argument("quantity", type=int, help="новый остаток")
    p.add_argument("--db", default=str(DB_FILE))
    p.set_defaults(func=cmd_stock)

    return parser


def run(argv: list[str] | None = None) -> int:
    load_env()
    args = build_parser().parse_args(argv)
    if not args.command:
        return cmd_menu(args)
    return args.func(args)


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
