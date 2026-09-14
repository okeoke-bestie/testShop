"""Устойчивость к отказам внешних сервисов: ретраи и предохранитель.

Любой вызов по сети когда-нибудь отвалится — это не исключение,
а нормальный режим работы. Вопрос только в том, что сервис сделает
в этот момент.

Здесь два механизма, которые спрашивают почти всегда:
  * retry с экспоненциальной задержкой и джиттером;
  * circuit breaker — предохранитель.
"""

from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from .errors import ExternalServiceError

logger = logging.getLogger(__name__)


@dataclass
class RetryPolicy:
    """Политика повторов.

    Экспоненциальная задержка: 0.1 -> 0.2 -> 0.4 -> 0.8. Постоянная
    задержка плоха тем, что при перегрузке сервиса-получателя все
    клиенты продолжают долбить его с прежней частотой и не дают встать.

    Джиттер (случайная добавка) решает проблему «стада»: без него тысяча
    клиентов, упавших одновременно, повторит попытку тоже одновременно
    и положит сервис повторно. С джиттером повторы размазываются
    по времени.

    `retry_on` перечисляет исключения, которые ИМЕЕТ смысл повторять.
    Повторять ошибку валидации бессмысленно: она детерминирована
    и повторится ровно так же, только время потратим.
    """

    attempts: int = 3
    base_delay: float = 0.1
    max_delay: float = 2.0
    jitter: float = 0.1
    retry_on: tuple[type[Exception], ...] = (ExternalServiceError, TimeoutError, ConnectionError)

    def delay_for(self, attempt: int) -> float:
        delay = min(self.base_delay * (2**attempt), self.max_delay)
        return delay + random.uniform(0, self.jitter)


def with_retry(policy: RetryPolicy | None = None, sleep: Callable[[float], None] = time.sleep):
    """Декоратор повторов.

    `sleep` вынесен параметром, чтобы тесты не спали по-настоящему:
    подставляется заглушка, и тест на четыре попытки с задержками
    отрабатывает мгновенно. Прятать `time.sleep` внутри — значит сделать
    функцию непроверяемой.

    `functools.wraps` сохраняет имя и docstring обёрнутой функции.
    Без него в трейсбеках и в документации везде будет `wrapper`,
    а `help()` покажет описание декоратора.
    """
    policy = policy or RetryPolicy()

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error: Exception | None = None
            for attempt in range(policy.attempts):
                try:
                    return func(*args, **kwargs)
                except policy.retry_on as exc:
                    last_error = exc
                    if attempt == policy.attempts - 1:
                        break
                    pause = policy.delay_for(attempt)
                    logger.warning(
                        "%s упал (попытка %d/%d): %s. Повтор через %.2f с",
                        func.__name__, attempt + 1, policy.attempts, exc, pause,
                    )
                    sleep(pause)
            raise ExternalServiceError(
                f"{func.__name__} не отработал за {policy.attempts} попыток"
            ) from last_error

        return wrapper

    return decorator


class BreakerState(str, Enum):
    CLOSED = "closed"  # всё хорошо, запросы идут
    OPEN = "open"  # плохо, запросы не пускаем
    HALF_OPEN = "half_open"  # пробуем, восстановился ли


@dataclass
class CircuitBreaker:
    """Предохранитель.

    Зачем нужен, если уже есть ретраи: ретраи помогают при одиночном
    сбое, но делают только хуже, когда сервис лежит целиком. Каждый
    клиент тратит на упавший сервис свои таймауты, держит потоки
    занятыми — и падает сам, утягивая за собой соседей. Так локальная
    авария превращается в общую.

    Предохранитель это обрывает: после N ошибок подряд он размыкается
    и перестаёт пускать запросы вообще, отвечая ошибкой мгновенно.
    Через `recovery_time` пропускает одну пробную попытку: успех
    возвращает работу в норму, неудача снова размыкает.
    """

    failure_threshold: int = 5
    recovery_time: float = 30.0
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opened_at: float = field(default=0.0)
    clock: Callable[[], float] = time.monotonic

    def _can_attempt(self) -> bool:
        if self.state is BreakerState.CLOSED:
            return True
        if self.state is BreakerState.OPEN:
            if self.clock() - self.opened_at >= self.recovery_time:
                self.state = BreakerState.HALF_OPEN
                logger.info("Предохранитель: пробная попытка")
                return True
            return False
        return True  # HALF_OPEN: одна попытка разрешена

    def record_success(self) -> None:
        if self.state is not BreakerState.CLOSED:
            logger.info("Предохранитель замкнут обратно, сервис восстановился")
        self.state = BreakerState.CLOSED
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.state is BreakerState.HALF_OPEN or self.failures >= self.failure_threshold:
            self.state = BreakerState.OPEN
            self.opened_at = self.clock()
            logger.warning("Предохранитель разомкнут после %d ошибок", self.failures)

    def call(self, func: Callable, *args, **kwargs):
        if not self._can_attempt():
            raise ExternalServiceError(
                "предохранитель разомкнут, запрос не отправлен",
                state=self.state.value,
            )
        try:
            result = func(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


@dataclass
class TTLCache:
    """Кэш с временем жизни.

    Зачем вообще писать свой, когда есть `functools.lru_cache`: у lru_cache
    нет срока жизни. Для справочника валют или каталога это неприемлемо —
    значение протухает, а кэш продолжает его отдавать до перезапуска.

    Отдельная тонкость: ключ строится из аргументов, поэтому они должны
    быть хешируемыми. Список в аргументах сломает и lru_cache, и этот кэш —
    типовой вопрос про изменяемые и неизменяемые типы.
    """

    ttl: float = 60.0
    max_size: int = 1000
    clock: Callable[[], float] = time.monotonic
    _store: dict = field(default_factory=dict, init=False)
    hits: int = field(default=0, init=False)
    misses: int = field(default=0, init=False)

    def get(self, key):
        item = self._store.get(key)
        if item is None:
            self.misses += 1
            return None
        value, expires_at = item
        if self.clock() >= expires_at:
            # Протухшее значение удаляем сразу, иначе словарь растёт
            # мусором, который никто не вычистит.
            del self._store[key]
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key, value) -> None:
        if len(self._store) >= self.max_size:
            # Простейшее вытеснение: выкидываем самый старый ключ.
            # dict в Python 3.7+ хранит порядок вставки, поэтому
            # первый ключ — самый давний.
            oldest = next(iter(self._store))
            del self._store[oldest]
        self._store[key] = (value, self.clock() + self.ttl)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def clear(self) -> None:
        self._store.clear()
