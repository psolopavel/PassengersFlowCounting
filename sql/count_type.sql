-- count_type: розділення цілоденного та кінцевих зупинок у сесіях і агрегаті.
-- schedules.type лишається whole_day / final_stops; у сесіях/summary — full_day / finish_stops.
--
-- Скрипт ІДЕМПОТЕНТНИЙ: можна запускати повторно. Додає колонки, лише якщо їх
-- ще немає, та замінює старий унікальний ключ uq_summary_car_day (car_id, count_day)
-- на uq_summary_car_day_type (car_id, count_day, count_type) — інакше другий рядок
-- (інший count_type) за той самий день не створюється (IntegrityError).

-- 1) counting_sessions.count_type ------------------------------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE()
             AND TABLE_NAME = 'counting_sessions'
             AND COLUMN_NAME = 'count_type');
SET @sql := IF(@c = 0,
    "ALTER TABLE counting_sessions ADD COLUMN count_type ENUM('full_day','finish_stops') NOT NULL DEFAULT 'full_day' AFTER kind",
    'SELECT 1');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;

-- 2) counting_daily_summary.count_type -------------------------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE()
             AND TABLE_NAME = 'counting_daily_summary'
             AND COLUMN_NAME = 'count_type');
SET @sql := IF(@c = 0,
    "ALTER TABLE counting_daily_summary ADD COLUMN count_type ENUM('full_day','finish_stops') NOT NULL DEFAULT 'full_day' AFTER kind",
    'SELECT 1');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;

-- 3) Прибрати старий унікальний ключ (car_id, count_day), якщо він ще є ----------
SET @i := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE()
             AND TABLE_NAME = 'counting_daily_summary'
             AND INDEX_NAME = 'uq_summary_car_day');
SET @sql := IF(@i > 0,
    'ALTER TABLE counting_daily_summary DROP INDEX uq_summary_car_day',
    'SELECT 1');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;

-- 4) Додати новий унікальний ключ (car_id, count_day, count_type), якщо його нема -
SET @i := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE()
             AND TABLE_NAME = 'counting_daily_summary'
             AND INDEX_NAME = 'uq_summary_car_day_type');
SET @sql := IF(@i = 0,
    'ALTER TABLE counting_daily_summary ADD UNIQUE KEY uq_summary_car_day_type (car_id, count_day, count_type)',
    'SELECT 1');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;
