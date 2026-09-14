-- Начальная схема магазина.
--
-- Про типы:
--   BIGINT GENERATED ALWAYS AS IDENTITY — современная замена SERIAL.
--       SERIAL создаёт отдельную последовательность и права на неё
--       приходится выдавать отдельно; IDENTITY описан в стандарте SQL
--       и не даёт случайно вставить своё значение в ключ.
--   Деньги — BIGINT в копейках. NUMERIC тоже подошёл бы и был бы точен,
--       но целое быстрее и не оставляет соблазна написать 0.1 + 0.2.
--   TIMESTAMPTZ, а не TIMESTAMP: без таймзоны время становится
--       бессмысленным, как только появляется второй сервер.

CREATE TABLE IF NOT EXISTS products (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku           TEXT        NOT NULL UNIQUE,
    title         TEXT        NOT NULL,
    description   TEXT        NOT NULL DEFAULT '',
    category      TEXT        NOT NULL DEFAULT 'other',
    price_kopecks BIGINT      NOT NULL CHECK (price_kopecks >= 0),
    stock         INTEGER     NOT NULL CHECK (stock >= 0),
    version       INTEGER     NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Составной индекс под keyset-пагинацию: сортировка идёт по (title, id),
-- и база отдаёт страницу без сортировки всей таблицы.
CREATE INDEX IF NOT EXISTS idx_products_title_id ON products (title, id);
CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);

CREATE TABLE IF NOT EXISTS orders (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id     BIGINT      NOT NULL,
    status          TEXT        NOT NULL
                    CHECK (status IN ('new', 'paid', 'shipped', 'cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key TEXT,
    request_hash    TEXT
);

-- CHECK, а не ENUM: добавить значение в ENUM — это ALTER TYPE, который
-- до Postgres 12 не работал внутри транзакции и до сих пор необратим.
-- CHECK правится обычной миграцией.

-- Частичный уникальный индекс: уникальность требуется только для
-- заполненных ключей. Обычный UNIQUE в Postgres допускает несколько
-- NULL, но частичный индекс выражает намерение явно.
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency
    ON orders (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders (customer_id, id DESC);

CREATE TABLE IF NOT EXISTS order_lines (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id      BIGINT  NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id    BIGINT  NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    sku           TEXT    NOT NULL,
    title         TEXT    NOT NULL DEFAULT '',
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    price_kopecks BIGINT  NOT NULL
);

-- Без этого индекса выборка строк заказа делает полный перебор,
-- и «загрузить 50 заказов со строками» превращается в 50 сканов.
CREATE INDEX IF NOT EXISTS idx_order_lines_order ON order_lines (order_id);

-- ON DELETE RESTRICT на товар — намеренно: удалить товар, на который
-- ссылаются заказы, нельзя. История заказов важнее удобства чистки
-- каталога; товар помечают снятым с продажи, а не удаляют.
