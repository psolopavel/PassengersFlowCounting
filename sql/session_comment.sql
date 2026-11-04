-- =====================================================================
-- Миграция: нотатка рахувальника к сессии при любом статусе.
--   1) counting_daily_summary.comment — копия data.comment для быстрого доступа;
--   2) counting_sessions.data: ключ skip_comment → comment;
--   3) заполнить counting_daily_summary.comment для уже существующих строк.
--
-- updated_at / computed_at присваиваются сами себе — иначе ON UPDATE
-- CURRENT_TIMESTAMP сдвинет «коли відбувся» в статистике, просрочку и выбор
-- канонической ready-сессии.
--
-- Скрипт можно запускать повторно.
-- Запускать под пользователем с полным доступом к PassengersFlowCounting.
-- =====================================================================

USE PassengersFlowCounting;

-- 1) counting_daily_summary.comment ------------------------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE()
             AND TABLE_NAME = 'counting_daily_summary'
             AND COLUMN_NAME = 'comment');
SET @sql := IF(@c = 0,
    'ALTER TABLE counting_daily_summary ADD COLUMN comment TEXT NULL DEFAULT NULL AFTER trips_json',
    'SELECT 1');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;

-- 2) data.skip_comment → data.comment ----------------------------------------
--   CASE вместо AND: порядок вычисления условий в WHERE не гарантирован,
--   а JSON_CONTAINS_PATH падает на невалидном JSON.
UPDATE `counting_sessions`
SET `data` = JSON_REMOVE(
        JSON_SET(`data`, '$.comment', JSON_UNQUOTE(JSON_EXTRACT(`data`, '$.skip_comment'))),
        '$.skip_comment'
    ),
    `updated_at` = `updated_at`
WHERE (CASE WHEN JSON_VALID(`data`)
            THEN JSON_CONTAINS_PATH(`data`, 'one', '$.skip_comment')
            ELSE 0 END) = 1;

-- 3) counting_daily_summary.comment из сессии --------------------------------
UPDATE `counting_daily_summary` s
JOIN `counting_sessions` cs ON cs.`id` = s.`session_id`
SET s.`comment` = NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(cs.`data`, '$.comment'))), ''),
    s.`computed_at` = s.`computed_at`
WHERE (CASE WHEN JSON_VALID(cs.`data`)
            THEN JSON_TYPE(JSON_EXTRACT(cs.`data`, '$.comment')) = 'STRING'
            ELSE 0 END) = 1;
