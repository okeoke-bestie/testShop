-- Витрина выросла: у товаров появилась платформа и справочные поля,
-- у магазина — раздел отзывов.
--
-- Почему отдельным файлом, а не правкой 001: применённые миграции
-- не переписывают. Runner хранит контрольную сумму каждого файла,
-- и изменение уже применённого файла он отвергнет — иначе схема
-- на разных стендах разъедется молча, а это чинится дольше,
-- чем любая ошибка в SQL.

-- ---------- товары ----------

ALTER TABLE products ADD COLUMN IF NOT EXISTS platform  TEXT    NOT NULL DEFAULT '';
ALTER TABLE products ADD COLUMN IF NOT EXISTS genre     TEXT    NOT NULL DEFAULT '';
ALTER TABLE products ADD COLUMN IF NOT EXISTS developer TEXT    NOT NULL DEFAULT '';
ALTER TABLE products ADD COLUMN IF NOT EXISTS year      INTEGER NOT NULL DEFAULT 0;

-- Фильтр каталога по платформе — самый частый запрос витрины.
CREATE INDEX IF NOT EXISTS idx_products_platform ON products (platform);

-- ---------- отзывы ----------

CREATE TABLE IF NOT EXISTS reviews (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- SET NULL, а не CASCADE: отзыв о снятом с продажи товаре остаётся
    -- мнением покупателя о магазине. Поэтому колонка NULLable.
    product_id BIGINT      REFERENCES products (id) ON DELETE SET NULL,
    author     TEXT        NOT NULL CHECK (length(author) > 0),
    rating     SMALLINT    NOT NULL CHECK (rating BETWEEN 1 AND 5),
    title      TEXT        NOT NULL DEFAULT '',
    body       TEXT        NOT NULL CHECK (length(body) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Демонстрационные отзывы помечены явно, чтобы их нельзя было
    -- спутать с настоящими и чтобы их можно было выборочно удалить.
    is_demo    BOOLEAN     NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_reviews_product ON reviews (product_id, id);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews (created_at DESC);
