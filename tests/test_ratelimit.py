"""Ограничение частоты запросов.

Часы во всех тестах подставные. Тест, который честно спит секунду,
чтобы проверить окно в секунду, — это тест, который ничего не проверяет
про границу окна, зато делает прогон медленным.
"""

from __future__ import annotations

import threading

import pytest

from shopapi.errors import TooManyRequests
from shopapi.ratelimit import Limiter, Rule, client_ip


class FakeClock:
    """Часы, которые идут только когда их двигают."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(clock):
    return Limiter(clock=clock)


def test_rule_rejects_nonsense():
    """Неправильно настроенный лимит должен падать при создании.

    Rule(limit=0) пропустил бы ноль запросов, то есть закрыл бы ручку
    насовсем. Такую ошибку надо ловить при старте, а не по жалобам.
    """
    with pytest.raises(ValueError):
        Rule(limit=0, window_seconds=60)
    with pytest.raises(ValueError):
        Rule(limit=5, window_seconds=0)


def test_allows_up_to_the_limit(limiter):
    rule = Rule(limit=3, window_seconds=60)
    assert limiter.check("ip:login", rule) == 0.0
    assert limiter.check("ip:login", rule) == 0.0
    assert limiter.check("ip:login", rule) == 0.0
    assert limiter.check("ip:login", rule) > 0.0


def test_window_slides(limiter, clock):
    """Освобождается место ровно тогда, когда старая попытка выпала.

    Это и есть разница со счётчиком на фиксированном окне: там надо
    было бы ждать конца минуты целиком.
    """
    rule = Rule(limit=2, window_seconds=60)
    limiter.check("ip", rule)          # t=0
    clock.advance(30)
    limiter.check("ip", rule)          # t=30
    assert limiter.check("ip", rule) > 0.0

    clock.advance(31)                  # t=61: первая попытка выпала из окна
    assert limiter.check("ip", rule) == 0.0


def test_retry_after_points_at_the_oldest_hit(limiter, clock):
    """Ждать надо до освобождения места, а не целое окно.

    Если вернуть window целиком, вежливый клиент прождёт вдвое дольше
    нужного — и это единственное, что он может сделать, потому что
    другого числа ему не сообщили.
    """
    rule = Rule(limit=1, window_seconds=60)
    limiter.check("ip", rule)
    clock.advance(59)
    wait = limiter.check("ip", rule)
    assert 0 < wait <= 1.01


def test_keys_are_independent(limiter):
    """Разные действия и разные адреса считаются порознь."""
    rule = Rule(limit=1, window_seconds=60)
    assert limiter.check("1.1.1.1:login", rule) == 0.0
    assert limiter.check("1.1.1.1:login", rule) > 0.0
    assert limiter.check("1.1.1.1:register", rule) == 0.0
    assert limiter.check("2.2.2.2:login", rule) == 0.0


def test_block_seconds_cools_the_source_down(limiter, clock):
    """После исчерпания лимита источник блокируется на отдельный срок.

    Смысл в подборе паролей: без блокировки перебор идёт со скоростью
    «лимит в окно» бесконечно долго, что за ночь даёт много попыток.
    """
    rule = Rule(limit=2, window_seconds=10, block_seconds=300)
    limiter.check("ip", rule)
    limiter.check("ip", rule)
    assert limiter.check("ip", rule) == pytest.approx(300)

    # Окно уже прошло, но блокировка ещё нет.
    clock.advance(11)
    assert limiter.check("ip", rule) > 0.0

    clock.advance(300)
    assert limiter.check("ip", rule) == 0.0


def test_hit_raises_with_retry_after(limiter):
    rule = Rule(limit=1, window_seconds=60)
    limiter.hit("ip", rule)
    with pytest.raises(TooManyRequests) as info:
        limiter.hit("ip", rule)
    assert info.value.http_status == 429
    # Округление ВВЕРХ: 0.4 секунды -> «повторите через 1», а не «через 0».
    # Ноль клиент прочитает как «можно прямо сейчас» и придёт снова.
    assert info.value.retry_after >= 1


def test_purge_drops_silent_keys(limiter, clock):
    """Словарь не должен расти вечно.

    На публичном сайте каждый новый адрес — это новый ключ; без чистки
    это медленная утечка памяти.
    """
    rule = Rule(limit=5, window_seconds=60)
    for i in range(50):
        limiter.check(f"10.0.0.{i}", rule)
    assert limiter.tracked_keys == 50

    clock.advance(3601)
    limiter.purge()
    assert limiter.tracked_keys == 0


def test_purge_keeps_active_keys(limiter, clock):
    rule = Rule(limit=5, window_seconds=60)
    limiter.check("old", rule)
    clock.advance(3601)
    limiter.check("fresh", rule)
    limiter.purge()
    assert limiter.tracked_keys == 1


def test_counting_is_thread_safe(limiter):
    """Проверка и списание — одна операция под замком.

    Если сделать их двумя, параллельные запросы прочитают счётчик
    до того, как кто-то из них его увеличит, и все пройдут.
    """
    rule = Rule(limit=100, window_seconds=600)
    passed = []
    lock = threading.Lock()

    def worker():
        allowed = limiter.check("shared", rule) == 0.0
        with lock:
            passed.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(300)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(passed) == 100


class FakeRequest:
    def __init__(self, host="7.7.7.7", headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


def test_client_ip_ignores_forwarded_header_by_default():
    """Без доверия к прокси заголовок не читается.

    Иначе лимит обходится одной строкой: подставил случайный
    X-Forwarded-For — получил чистый счётчик.
    """
    request = FakeRequest(headers={"X-Forwarded-For": "1.2.3.4"})
    assert client_ip(request) == "7.7.7.7"


def test_client_ip_reads_forwarded_header_behind_proxy():
    """Один свой прокси: настоящий адрес — последний в цепочке."""
    request = FakeRequest(headers={"X-Forwarded-For": "203.0.113.7"})
    assert client_ip(request, trust_proxy=True) == "203.0.113.7"


def test_spoofed_forwarded_header_does_not_win():
    """Главный тест этого файла.

    Прокси не заменяют X-Forwarded-For, а ДОПИСЫВАЮТ себя справа,
    сохраняя присланное клиентом. Клиент, отправивший
    «X-Forwarded-For: 1.2.3.4», приходит в приложение как
    «1.2.3.4, <его настоящий адрес>».

    Код, берущий первый элемент, поверит подделке — и лимит обойдётся
    сменой одной строки в заголовке на каждый запрос. Считать надо
    справа, по числу своих прокси.
    """
    request = FakeRequest(headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7"})
    assert client_ip(request, trust_proxy=True, hops=1) == "203.0.113.7"


def test_two_proxies_shift_the_position():
    """Два своих прокси — настоящий адрес предпоследний."""
    request = FakeRequest(
        headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7, 10.0.0.1"},
    )
    assert client_ip(request, trust_proxy=True, hops=2) == "203.0.113.7"


def test_short_chain_does_not_break_indexing():
    """Цепочка короче ожидаемой не должна ронять запрос.

    Так бывает при внутренних проверках и при смене схемы прокси.
    Берётся самый левый доступный адрес, а не IndexError.
    """
    request = FakeRequest(headers={"X-Forwarded-For": "203.0.113.7"})
    assert client_ip(request, trust_proxy=True, hops=3) == "203.0.113.7"


def test_trusted_header_wins_over_the_chain():
    """Заголовок периметра надёжнее: его перезаписывают, а не дописывают."""
    request = FakeRequest(headers={
        "X-Forwarded-For": "1.2.3.4, 10.0.0.1",
        "CF-Connecting-IP": "203.0.113.7",
    })
    assert client_ip(
        request, trust_proxy=True, trusted_header="CF-Connecting-IP",
    ) == "203.0.113.7"


def test_trusted_header_is_ignored_without_proxy_trust():
    request = FakeRequest(headers={"CF-Connecting-IP": "1.2.3.4"})
    assert client_ip(request, trusted_header="CF-Connecting-IP") == "7.7.7.7"


def test_empty_chain_falls_back_to_the_socket():
    """Пустой или мусорный заголовок — не повод потерять адрес."""
    request = FakeRequest(headers={"X-Forwarded-For": " , ,"})
    assert client_ip(request, trust_proxy=True) == "7.7.7.7"


def test_client_ip_survives_missing_client():
    """У запроса может не быть клиента — например, во внутреннем вызове."""
    request = FakeRequest()
    request.client = None
    assert client_ip(request) == "unknown"
