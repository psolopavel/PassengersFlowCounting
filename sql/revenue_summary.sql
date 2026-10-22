-- =====================================================================
-- Миграция: агрегат «день-машина» для внешнего API выручки.
--   counting_daily_summary — компактная материализация одной ready-сессии:
--     distance_km, passengers_total, trips_json (FIFO-разбор межгорода).
--   Заполняется при завершении сессии (см. summary_service) и бэкофиллом
--   (scripts/backfill_summaries.py). Выручка НЕ хранится — считается на
--   чтении из trips_json по актуальному на count_day тарифу.
--
-- Запускать под пользователем с полным доступом к PassengersFlowCounting.
-- =====================================================================

USE PassengersFlowCounting;

CREATE TABLE IF NOT EXISTS `counting_daily_summary` (
    `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `session_id`       BIGINT UNSIGNED NOT NULL,
    `car_id`           BIGINT UNSIGNED NOT NULL,
    `imei`             VARCHAR(32)     NOT NULL,
    `count_day`        DATE            NOT NULL,
    `kind`             ENUM('city','intercity') NOT NULL,
    `status`           ENUM('in_progress','ready','skipped') NOT NULL,
    `distance_km`      DECIMAL(10,2)   NOT NULL DEFAULT 0,
    `passengers_total` INT UNSIGNED    NOT NULL DEFAULT 0,
    -- Межгород: JSON-массив завершённых поездок [{route_id, from_id, to_id, pax}].
    -- Город: NULL (деньги не считаем).
    `trips_json`       LONGTEXT        NULL,
    `computed_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    -- Одна каноническая строка на день-машину (при нескольких сессиях —
    -- выбирается ready с максимальным updated_at, см. summary_service).
    UNIQUE KEY `uq_summary_car_day` (`car_id`, `count_day`),
    KEY `idx_summary_imei_day` (`imei`, `count_day`),
    KEY `idx_summary_day` (`count_day`),
    CONSTRAINT `fk_summary_car`
        FOREIGN KEY (`car_id`) REFERENCES `cars` (`id`)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
