-- =====================================================================
-- Миграция: удаление рахувальника не должно ломаться и не должно стирать
-- его подсчёты.
--
--   Ошибка 1451 при DELETE FROM counters: FK counting_sessions.counter_id
--   создан без ON DELETE. CASCADE здесь не подходит — на сессию ссылаются
--   counting_daily_summary.session_id (внешний API) и
--   report_deliveries.session_id (почта), удаление сессий сломало бы
--   выдачу API и повторную отправку/скачивание отчётов.
--
--   Поэтому counter_id становится NULL-able, FK — ON DELETE SET NULL:
--   сессия остаётся, просто без автора («видалений рахувальник»).
--
-- Запускать под пользователем с полным доступом к PassengersFlowCounting.
-- =====================================================================

USE PassengersFlowCounting;

-- ---------------------------------------------------------------------
-- ШАГ 1. Снять все существующие FK по counter_id (на сервере это
--   fk_cs_counter, в старой миграции — fk_session_counter; могут быть оба).
-- ---------------------------------------------------------------------
SET @drops := (
    SELECT GROUP_CONCAT(CONCAT('DROP FOREIGN KEY `', CONSTRAINT_NAME, '`') SEPARATOR ', ')
    FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'counting_sessions'
      AND COLUMN_NAME = 'counter_id'
      AND REFERENCED_TABLE_NAME IS NOT NULL
);
SET @sql := IF(@drops IS NULL, 'SELECT 1',
               CONCAT('ALTER TABLE `counting_sessions` ', @drops));
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------------
-- ШАГ 2. Разрешить NULL.
-- ---------------------------------------------------------------------
ALTER TABLE `counting_sessions`
    MODIFY `counter_id` BIGINT UNSIGNED NULL;

-- ---------------------------------------------------------------------
-- ШАГ 3. Вернуть FK с ON DELETE SET NULL.
-- ---------------------------------------------------------------------
ALTER TABLE `counting_sessions`
    ADD CONSTRAINT `fk_cs_counter`
    FOREIGN KEY (`counter_id`) REFERENCES `counters` (`user_id`)
    ON DELETE SET NULL ON UPDATE CASCADE;
