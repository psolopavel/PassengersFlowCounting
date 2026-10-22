-- =====================================================================
-- Додаткові поля cars: пріоритет медіа та підрахунок «сьогодні».
-- (Якщо колонки вже додані вручну — скрипт можна не запускати.)
-- =====================================================================

USE PassengersFlowCounting;

ALTER TABLE `cars`
    ADD COLUMN IF NOT EXISTS `source_priority`
        ENUM('photo','video') NOT NULL DEFAULT 'video'
        AFTER `count_source`;

ALTER TABLE `cars`
    ADD COLUMN IF NOT EXISTS `non_shedule_count`
        TINYINT(1) NOT NULL DEFAULT 0
        AFTER `source_priority`;
