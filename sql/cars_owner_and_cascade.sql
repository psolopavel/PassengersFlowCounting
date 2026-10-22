-- =====================================================================
-- Миграция:
--   1) добавить cars.owner_user_id — связь с аккаунтом Joomla (jos_users.id);
--   2) починить внешний ключ schedules.car_id, чтобы удаление машины
--      каскадно удаляло её расписания (ошибка 1451 при DELETE FROM cars).
--
-- Запускать под пользователем с полным доступом к PassengersFlowCounting.
-- =====================================================================

USE PassengersFlowCounting;

-- ---------------------------------------------------------------------
-- ШАГ 1. Колонка владельца машины.
--   Мягкая (без жёсткого FK) межбазовая связь с joomla.jos_users(id).
--   Тип INT — как у jos_users.id (int(11)). NULL — чтобы существующие
--   строки прошли миграцию; в приложении владелец обязателен.
-- ---------------------------------------------------------------------
ALTER TABLE `cars`
    ADD COLUMN `owner_user_id` INT NULL DEFAULT NULL AFTER `cameras`,
    ADD INDEX `idx_cars_owner_user_id` (`owner_user_id`);

-- (опционально) заполнить владельца по совпадению company_name с именем
-- аккаунта в joomla — если обе БД на одном сервере и есть доступ на чтение:
-- UPDATE `cars` c
-- JOIN `joomla`.`jos_users` u ON u.`name` = c.`company_name`
-- SET c.`owner_user_id` = u.`id`
-- WHERE c.`owner_user_id` IS NULL;

-- ---------------------------------------------------------------------
-- ШАГ 2. Пересоздать FK schedules.car_id с ON DELETE CASCADE.
--   Имя ограничения из ошибки: fk_sched_car. Если у вас другое —
--   посмотрите его так:
--   SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE
--   WHERE TABLE_SCHEMA='PassengersFlowCounting' AND TABLE_NAME='schedules'
--     AND COLUMN_NAME='car_id' AND REFERENCED_TABLE_NAME='cars';
-- ---------------------------------------------------------------------
ALTER TABLE `schedules` DROP FOREIGN KEY `fk_sched_car`;

ALTER TABLE `schedules`
    ADD CONSTRAINT `fk_sched_car`
    FOREIGN KEY (`car_id`) REFERENCES `cars` (`id`)
    ON DELETE CASCADE ON UPDATE CASCADE;

-- ---------------------------------------------------------------------
-- (Необязательно) то же для привязки считальщика, чтобы удаление
-- считальщика тоже чистило его расписания. Раскомментируйте при желании:
-- ---------------------------------------------------------------------
ALTER TABLE `schedules` DROP FOREIGN KEY `fk_sched_counter`;
ALTER TABLE `schedules`
    ADD CONSTRAINT `fk_sched_counter`
    FOREIGN KEY (`counter_id`) REFERENCES `users` (`id`)
    ON DELETE CASCADE ON UPDATE CASCADE;
