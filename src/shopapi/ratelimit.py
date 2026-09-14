"""Ограничение частоты запросов.

Зачем это появилось: до публикации в интернет ограничение было не нужно —
сервис открывали на localhost. Как только у сайта есть публичный адрес,
к нему приходят не только люди: формы регистрации и входа перебирают
автоматически, а бесплатная база живёт в пределах полугигабайта, и
забить её мусорными профилями можно за вечер.

Алгоритм — скользящее окно по списку отметок времени. Он выбран
не потому, что самый хитрый, а потому, что самый честный: «не больше N
запросов за последние T секунд» означает ровно это. У счётчика
с фиксированным окном есть известная дыра на стыке: 10 запросов в конце
одной минуты и 10 в начале следующей — это 20 запросов за две секунды,
формально не нарушивших лимит.

Состояние лежит в памяти процесса. Это осознанное ограничение, а не
недосмотр: на одном экземпляре сервиса (а бесплатный тариф хостинга
другого и не даёт) этого достаточно, а на нескольких счётчики разъедутся
и каждый начнёт пропускать свою долю. Правильное решение для нескольких
экземпляров — общий Redis с атомарным INCR; менять придётся только
внутренности `Limiter`, интерфейс останется тем же.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from .errors import TooManyRequests


@dataclass(frozen=True)
class Rule:
    """Сколько запросов и за какое окно.

    `block_seconds` — отдельная величина: после исчерпания лимита можно
    не просто отказывать, пока окно не сдвинется, а «остудить» источник
    на подольше. Для перебора паролей это заметная разница.
    """

    limit: int
    window_seconds: float
    block_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit должен быть не меньше 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds должен быть больше нуля")


class Limiter:
    """Скользящее окно на каждый ключ.

    Ключ — это «что ограничиваем»: обычно `ip:действие`. Разные действия
    считаются отдельно, иначе десять просмотров каталога съели бы
    попытки входа.
    """

    def __init__(self, clock=time.monotonic):
        # monotonic, а не time(): системные часы могут прыгнуть назад
        # (синхронизация NTP, перевод времени), и тогда окно
        # схлопнется или зависнет. Монотонные часы назад не идут.
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, key: str, rule: Rule) -> float:
        """Возвращает 0.0, если можно, иначе — через сколько секунд повторять.

        Метод и проверяет, и засчитывает попытку: две отдельные операции
        («посмотреть» и «списать») — это гонка, в которую пролезут
        параллельные запросы.
        """
        now = self._clock()
        with self._lock:
            blocked = self._blocked_until.get(key)
            if blocked is not None:
                if blocked > now:
                    return blocked - now
                del self._blocked_until[key]

            hits = self._hits.setdefault(key, deque())
            cutoff = now - rule.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= rule.limit:
                if rule.block_seconds:
                    self._blocked_until[key] = now + rule.block_seconds
                    return rule.block_seconds
                # Ждать надо до того момента, когда самая старая отметка
                # выпадет из окна, — не «ещё одно окно целиком».
                return max(hits[0] + rule.window_seconds - now, 0.001)

            hits.append(now)
            return 0.0

    def hit(self, key: str, rule: Rule) -> None:
        """То же, но сразу бросает 429. Так удобнее в ручках."""
        wait = self.check(key, rule)
        if wait:
            raise TooManyRequests(
                "слишком много попыток, попробуйте чуть позже",
                retry_after=max(1, int(wait + 0.999)),
            )

    def reset(self, key: str | None = None) -> None:
        """Сброс — нужен тестам и ручному вмешательству."""
        with self._lock:
            if key is None:
                self._hits.clear()
                self._blocked_until.clear()
            else:
                self._hits.pop(key, None)
                self._blocked_until.pop(key, None)

    def purge(self) -> int:
        """Выбрасывает ключи, по которым давно ничего не было.

        Без этого словарь растёт на каждый новый адрес и не уменьшается
        никогда — медленная, но верная утечка памяти на публичном сайте.
        Вызывается фоновой задачей, а не на каждом запросе: чистка под
        общим замком на горячем пути стоила бы дороже, чем экономит.
        """
        now = self._clock()
        with self._lock:
            dead = [k for k, hits in self._hits.items() if not hits or hits[-1] < now - 3600]
            for key in dead:
                del self._hits[key]
            expired = [k for k, until in self._blocked_until.items() if until <= now]
            for key in expired:
                del self._blocked_until[key]
        return len(dead) + len(expired)

    @property
    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._hits)


def client_ip(
    request,
    trust_proxy: bool = False,
    hops: int = 1,
    trusted_header: str | None = None,
) -> str:
    """Адрес клиента.

    Заголовки читаются ТОЛЬКО когда сервис заведомо стоит за обратным
    прокси (`trust_proxy`). Иначе их ставит кто угодно, и лимит
    обходится подстановкой случайного значения на каждый запрос.

    **Почему не первый адрес слева.** Очевидное «взять `split(",")[0]`»
    — распространённая и опасная ошибка. Прокси не заменяют
    `X-Forwarded-For`, а ДОПИСЫВАЮТ в него свой адрес справа, сохраняя
    то, что прислал клиент. Значит, клиент, отправивший
    `X-Forwarded-For: 1.2.3.4`, получит на входе в приложение
    `1.2.3.4, <его настоящий адрес>` — и код, берущий первый элемент,
    поверит подделке. Это ровно тот обход лимита, ради защиты
    от которого заголовок и читают.

    Считать надо СПРАВА, зная, сколько своих прокси стоит перед
    сервисом. При одном прокси настоящий адрес — последний в списке,
    при двух — предпоследний. Отсюда `parts[-hops]`.

    `trusted_header` — запасной, более надёжный путь. Некоторые
    периметры (например, Cloudflare) кладут адрес в собственный
    заголовок и ПЕРЕЗАПИСЫВАЮТ его, а не дописывают: подделать такой
    снаружи нельзя. Если он настроен и пришёл — берётся он.
    """
    if trust_proxy:
        if trusted_header:
            value = (request.headers.get(trusted_header) or "").strip()
            if value:
                return value

        forwarded = request.headers.get("X-Forwarded-For", "")
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            index = max(len(parts) - max(hops, 1), 0)
            return parts[index]

    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"
