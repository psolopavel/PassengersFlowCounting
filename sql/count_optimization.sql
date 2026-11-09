-- =====================================================================
-- Оптимізація підрахунку міжміста (cars.count_optimization).
--   none               — без оптимізації;
--   auto_gap_filling   — автозаповнення пропусків треку за маршрутами
--                        (замінює стару колонку auto_gap_filling);
--   manual_chronology  — рахувальник сам будує хронологію ходок і зупинок
--                        за відео, трек для розбивки на відрізки не використовується.
-- Також видаляється невикористовувана колонка alarms_only.
--
-- Порядок важливий: спочатку переносимо значення auto_gap_filling, потім видаляємо.
-- =====================================================================

USE PassengersFlowCounting;

ALTER TABLE `cars`
    ADD COLUMN IF NOT EXISTS `count_optimization`
        ENUM('none','auto_gap_filling','manual_chronology') NOT NULL DEFAULT 'none'
        AFTER `non_shedule_count`;

UPDATE `cars`
SET `count_optimization` = 'auto_gap_filling'
WHERE `auto_gap_filling` = 1;

ALTER TABLE `cars`
    DROP COLUMN IF EXISTS `auto_gap_filling`,
    DROP COLUMN IF EXISTS `alarms_only`;
