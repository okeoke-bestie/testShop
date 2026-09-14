# Образ для публичного размещения.
#
# slim, а не alpine: alpine собран на musl, и колёса psycopg/uvloop
# под него не готовы — pip начнёт собирать их из исходников, образ
# распухнет от компилятора, а сборка вырастет с секунд до минут.

FROM python:3.12-slim

# PYTHONUNBUFFERED — иначе вывод оседает в буфере, и логи хостинга
# показывают пустоту ровно до падения процесса. Разбирать инцидент
# по логам, которых нет, невозможно.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости ставятся ДО копирования кода: слой с ними кэшируется
# и не пересобирается при каждой правке исходников.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Приложение работает не от root. Если в сервисе найдётся дыра,
# она достанется пользователю без прав, а не хозяину контейнера.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

# Наполнить пустую базу каталогом при первом старте и поддерживать
# остатки: без этого публичная витрина за сутки станет списком
# «нет в наличии». Подробности — в src/shopapi/bootstrap.py.
ENV SHOP_AUTO_SEED=1 \
    SHOP_DEMO=1 \
    SHOP_TRUST_PROXY=1 \
    PORT=8080

EXPOSE 8080

# Проверка живости внутри образа: хостинг обычно стучится снаружи,
# но с ней `docker compose`, Kubernetes и локальный запуск понимают
# состояние контейнера одинаково.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; \
urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", 8080)}/healthz', timeout=4)"

# Форма с sh -c нужна ради подстановки $PORT: хостинг сообщает порт
# переменной окружения, а exec-форма CMD переменные не разворачивает.
#
# --proxy-headers + --forwarded-allow-ips: за обратным прокси клиентский
# адрес приходит заголовком. Без этих флагов в логах и в ограничителе
# частоты у всех посетителей будет один адрес — прокси.
CMD ["sh", "-c", "exec python -m uvicorn shopapi.api:app \
     --host 0.0.0.0 --port ${PORT:-8080} --app-dir src \
     --proxy-headers --forwarded-allow-ips='*'"]
