"""Тесты ретраев, предохранителя и кэша.

Ни один тест здесь не спит по-настоящему: время и задержки подменяются.
Тест, который ждёт три секунды, перестают запускать — а значит,
он бесполезен.
"""

import pytest

from shopapi.errors import ExternalServiceError, ValidationError
from shopapi.reliability import (
    BreakerState,
    CircuitBreaker,
    RetryPolicy,
    TTLCache,
    with_retry,
)

# ---------- ретраи ----------


def test_succeeds_without_retry():
    calls = []

    @with_retry(sleep=lambda _: None)
    def works():
        calls.append(1)
        return "ок"

    assert works() == "ок"
    assert len(calls) == 1


def test_retries_then_succeeds():
    calls = []

    @with_retry(RetryPolicy(attempts=3), sleep=lambda _: None)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ExternalServiceError("сервис моргнул")
        return "ок"

    assert flaky() == "ок"
    assert len(calls) == 3


def test_gives_up_after_attempts():
    calls = []

    @with_retry(RetryPolicy(attempts=3), sleep=lambda _: None)
    def broken():
        calls.append(1)
        raise ExternalServiceError("сервис лежит")

    with pytest.raises(ExternalServiceError):
        broken()
    assert len(calls) == 3


def test_does_not_retry_non_retryable_errors():
    """Повторять ошибку валидации бессмысленно.

    Она детерминирована: повторится ровно так же, только время
    потратим и клиента задержим.
    """
    calls = []

    @with_retry(RetryPolicy(attempts=5), sleep=lambda _: None)
    def bad_input():
        calls.append(1)
        raise ValidationError("некорректные данные")

    with pytest.raises(ValidationError):
        bad_input()
    assert len(calls) == 1


def test_delay_grows_exponentially():
    policy = RetryPolicy(base_delay=0.1, jitter=0.0, max_delay=10.0)

    assert policy.delay_for(0) == pytest.approx(0.1)
    assert policy.delay_for(1) == pytest.approx(0.2)
    assert policy.delay_for(2) == pytest.approx(0.4)


def test_delay_is_capped():
    """Без потолка задержка на десятой попытке уходит в минуты."""
    policy = RetryPolicy(base_delay=0.1, jitter=0.0, max_delay=1.0)
    assert policy.delay_for(20) == pytest.approx(1.0)


def test_jitter_spreads_retries():
    """Джиттер разводит повторы по времени.

    Без него тысяча клиентов, упавших одновременно, повторит попытку
    тоже одновременно и положит сервис повторно.
    """
    policy = RetryPolicy(base_delay=0.1, jitter=0.5)
    delays = {policy.delay_for(0) for _ in range(50)}

    assert len(delays) > 1, "джиттер не работает, задержки одинаковые"


def test_retry_preserves_function_metadata():
    """functools.wraps сохраняет имя и docstring.

    Без него в трейсбеках везде будет `wrapper`, а help() покажет
    описание декоратора вместо функции.
    """

    @with_retry(sleep=lambda _: None)
    def named_function():
        """Описание функции."""
        return 1

    assert named_function.__name__ == "named_function"
    assert named_function.__doc__ == "Описание функции."


# ---------- предохранитель ----------


def test_breaker_opens_after_threshold():
    breaker = CircuitBreaker(failure_threshold=3)

    for _ in range(3):
        with pytest.raises(ExternalServiceError):
            breaker.call(lambda: (_ for _ in ()).throw(ExternalServiceError("нет связи")))

    assert breaker.state is BreakerState.OPEN


def test_open_breaker_fails_fast_without_calling():
    """Разомкнутый предохранитель не вызывает функцию вообще.

    В этом весь смысл: не тратить таймауты и потоки на сервис,
    про который уже известно, что он лежит.
    """
    calls = []
    breaker = CircuitBreaker(failure_threshold=1)

    with pytest.raises(ExternalServiceError):
        breaker.call(lambda: (_ for _ in ()).throw(ExternalServiceError("нет связи")))

    with pytest.raises(ExternalServiceError):
        breaker.call(lambda: calls.append(1))

    assert calls == [], "функция вызвана при разомкнутом предохранителе"


def test_breaker_probes_after_recovery_time():
    """Через recovery_time пропускается одна пробная попытка."""
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_time=30.0, clock=lambda: now[0]
    )

    with pytest.raises(ExternalServiceError):
        breaker.call(lambda: (_ for _ in ()).throw(ExternalServiceError("нет связи")))
    assert breaker.state is BreakerState.OPEN

    now[0] = 31.0
    assert breaker.call(lambda: "восстановился") == "восстановился"
    assert breaker.state is BreakerState.CLOSED


def test_failed_probe_reopens_breaker():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, recovery_time=10.0, clock=lambda: now[0])

    with pytest.raises(ExternalServiceError):
        breaker.call(lambda: (_ for _ in ()).throw(ExternalServiceError("нет связи")))

    now[0] = 11.0
    with pytest.raises(ExternalServiceError):
        breaker.call(lambda: (_ for _ in ()).throw(ExternalServiceError("всё ещё лежит")))

    assert breaker.state is BreakerState.OPEN


# ---------- кэш ----------


def test_cache_returns_stored_value():
    cache = TTLCache(ttl=10.0)
    cache.set("k", "v")

    assert cache.get("k") == "v"
    assert cache.hits == 1


def test_cache_expires_by_ttl():
    """Ради чего свой кэш, а не functools.lru_cache: у того нет срока
    жизни, и протухшее значение отдаётся до перезапуска процесса."""
    now = [0.0]
    cache = TTLCache(ttl=5.0, clock=lambda: now[0])
    cache.set("k", "v")

    now[0] = 6.0
    assert cache.get("k") is None


def test_cache_evicts_oldest_when_full():
    cache = TTLCache(ttl=100.0, max_size=3)
    for i in range(4):
        cache.set(f"k{i}", i)

    assert cache.get("k0") is None, "старый ключ не вытеснен"
    assert cache.get("k3") == 3


def test_hit_rate_is_reported():
    cache = TTLCache(ttl=10.0)
    cache.set("k", "v")
    cache.get("k")
    cache.get("missing")

    assert cache.hit_rate == pytest.approx(0.5)


def test_cache_survives_empty_state():
    assert TTLCache().hit_rate == 0.0
