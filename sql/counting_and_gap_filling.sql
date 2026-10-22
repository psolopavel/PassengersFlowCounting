-- =====================================================================
-- Миграция для подсчёта пассажиров (город/межгород):
--   1) cars.auto_gap_filling — флаг починки трека по маршрутам;
--   2) counting_sessions — сессии подсчёта (одна машина за один день),
--      вся структура подсчёта хранится в JSON-поле data.
--
-- Запускать под пользователем с полным доступом к PassengersFlowCounting.
-- =====================================================================

USE PassengersFlowCounting;

-- ---------------------------------------------------------------------
-- ШАГ 1. Флаг автозаполнения разрывов трека.
--   1 — трек чинится по привязанным маршрутам перед разбором на отрезки.
--   Читается только для межгорода (город трек не использует).
-- ---------------------------------------------------------------------
ALTER TABLE `cars`
    ADD COLUMN `auto_gap_filling` TINYINT(1) NOT NULL DEFAULT 0 AFTER `alarms_only`;

-- ---------------------------------------------------------------------
-- ШАГ 2. Таблица сессий подсчёта.
--   status: in_progress (считается) / ready (готово) / skipped (пропущено).
--   data: JSON со всей структурой подсчёта (см. counting_build_service):
--     город      — одно число вошедших;
--     межгород   — периоды маршрутов + отрезки между остановками, каждому
--                  отрезку соответствует медиа и число людей в салоне.
--   Уникальность (counter_id, car_id, count_day) — одна сессия на день.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `counting_sessions` (
    `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `counter_id`     BIGINT UNSIGNED NOT NULL,
    `car_id`         BIGINT UNSIGNED NOT NULL,
    `count_day`      DATE            NOT NULL,
    `time_spent_sec` INT UNSIGNED    NOT NULL DEFAULT 0,
    `status`         ENUM('in_progress','ready','skipped') NOT NULL DEFAULT 'in_progress',
    `data`           LONGTEXT        NULL,
    `created_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_session_day` (`counter_id`, `car_id`, `count_day`),
    KEY `idx_session_car_day` (`car_id`, `count_day`),
    CONSTRAINT `fk_session_counter`
        FOREIGN KEY (`counter_id`) REFERENCES `users` (`id`)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT `fk_session_car`
        FOREIGN KEY (`car_id`) REFERENCES `cars` (`id`)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
