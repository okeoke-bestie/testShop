-- Настоящая регистрация: пароли и сессии.
--
-- До этой миграции покупатель выбирался из списка — вход изображался,
-- а не выполнялся. Теперь профиль создаёт сам человек, а выбрать
-- чужой нельзя.

-- ---------- пароль ----------

-- Одна колонка, а не три (алгоритм, соль, хеш): внутри строки лежит
-- `scrypt$соль$хеш`. Алгоритм записан в самой строке, поэтому его
-- можно сменить, не ломая уже существующие пароли — старые проверятся
-- по-старому, новые запишутся по-новому. С отдельными колонками
-- смена алгоритма означала бы сброс паролей у всех.
--
-- NULL допустим: у демонстрационных покупателей пароля нет, и войти
-- под ними невозможно. Это не забытое поле, а осознанное состояние.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS password_hash TEXT;

-- ---------- сессии ----------

CREATE TABLE IF NOT EXISTS sessions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Хеш токена, а не сам токен. Утечка этой таблицы не даёт войти:
    -- из хеша токен не восстановить. Та же мысль, что и с паролем.
    token_hash  TEXT        NOT NULL UNIQUE,
    customer_id BIGINT      NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Бессрочных сессий не бывает: украденный однажды токен работал бы
    -- вечно. Срок проверяется при каждом запросе.
    expires_at  TIMESTAMPTZ NOT NULL
);

-- CASCADE здесь уместен, в отличие от отзывов: сессия удалённого
-- покупателя бессмысленна сама по себе, а отзыв — нет.

CREATE INDEX IF NOT EXISTS idx_sessions_customer ON sessions (customer_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);
