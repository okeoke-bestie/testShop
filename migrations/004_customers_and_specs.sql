-- Покупатели, характеристики товара и подтверждённые отзывы.
--
-- Отдельным файлом, как и положено: применённые миграции не правят,
-- runner сверяет контрольные суммы и отвергнет изменённый файл.

-- ---------- характеристики товара ----------

-- Характеристики лежат ОДНОЙ колонкой в JSON, а не отдельной таблицей
-- «атрибут — значение». Причина практическая: у консоли, игры
-- и накопителя наборы характеристик не пересекаются вообще, а искать
-- и фильтровать по ним проект не умеет — они только показываются
-- на карточке. Городить EAV-таблицу ради вывода списка значит
-- получить join и сортировку там, где хватает одного поля.
--
-- Если завтра понадобится «показать все консоли с SSD от 1 ТБ»,
-- решение меняется: в PostgreSQL это JSONB с GIN-индексом, и вот
-- тогда тип колонки придётся сменить. Пока такой задачи нет.
ALTER TABLE products ADD COLUMN IF NOT EXISTS specs TEXT NOT NULL DEFAULT '{}';

-- ---------- покупатели ----------

CREATE TABLE IF NOT EXISTS customers (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT        NOT NULL CHECK (length(name) > 0),
    email        TEXT        NOT NULL UNIQUE,
    city         TEXT        NOT NULL DEFAULT '',
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_demo      BOOLEAN     NOT NULL DEFAULT false
);

-- Почта уникальна на уровне БАЗЫ, а не проверкой в коде. Проверка
-- «сначала SELECT, потом INSERT» — это гонка: два одновременных
-- запроса оба увидят, что почты нет, и оба вставят. Та же мысль,
-- что и с ключом идемпотентности у заказов.

-- ---------- отзывы от покупателей ----------

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS customer_id BIGINT
    REFERENCES customers (id) ON DELETE SET NULL;

-- «Покупка подтверждена» хранится отдельным полем, а не вычисляется
-- при каждом показе. Вычислять пришлось бы join с заказами на каждый
-- отзыв в списке, и это ровно та работа, которую делают один раз —
-- в момент, когда отзыв принимают.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_reviews_customer ON reviews (customer_id);

-- Один отзыв от покупателя на товар. Ограничение — в базе:
-- кнопку на витрине можно спрятать, но запрос никто не мешает
-- послать напрямую.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_one_per_customer
    ON reviews (customer_id, product_id)
 WHERE customer_id IS NOT NULL AND product_id IS NOT NULL;
