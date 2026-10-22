-- =====================================================================
-- Миграция: очередь отправки клиентских xlsx-отчётов (report_deliveries).
--
--   Строка ставится в очередь при материализации ready-сессии
--   (summary_service.safe_materialize_day -> report_delivery_service) только
--   для city / intercity с count_type = full_day. Отправляет отдельный демон
--   app/workers/report_mailer.py (systemd: passengersflow-mailer).
--
--   Одна строка = одна версия данных дня (data_hash). Повторные автосейвы с теми
--   же данными новых строк не создают; изменились данные — старая pending-строка
--   отменяется (cancelled), создаётся новая.
--
--   version присваивается в момент успешной отправки:
--   MAX(version среди sent за car_id + count_day + count_type) + 1.
--   Поэтому отправленные строки удалять не нужно — по ним считается номер версии.
--
-- Запускать под пользователем с полным доступом к PassengersFlowCounting.
-- =====================================================================

USE PassengersFlowCounting;

CREATE TABLE IF NOT EXISTS `report_deliveries` (
    `id`                  BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    `session_id`          BIGINT UNSIGNED  NOT NULL,
    `car_id`              BIGINT UNSIGNED  NOT NULL,
    `imei`                VARCHAR(32)      NOT NULL DEFAULT '',
    `count_day`           DATE             NOT NULL,
    `count_type`          ENUM('full_day','finish_stops') NOT NULL DEFAULT 'full_day',
    `kind`                ENUM('city','intercity') NOT NULL,
    -- sha256 значимой части data (числа подсчёта, отрезки, пробег).
    `data_hash`           CHAR(64)         NOT NULL,
    -- Номер версии отчёта за день; NULL, пока не отправлено.
    `version`             INT UNSIGNED     NULL,
    -- JSON-массив валидных адресов: ["a@ukr.net", "b@gmail.com"].
    -- Заполняется при постановке в очередь и обновляется перед отправкой.
    `recipient_emails`    JSON             NULL,
    -- JSON-массив адресов из gps.user_settings, не прошедших валидацию.
    `skipped_emails`      JSON             NULL,
    `status`              ENUM('pending','sending','sent','failed','cancelled','no_recipient')
                          NOT NULL DEFAULT 'pending',
    `attempts`            TINYINT UNSIGNED NOT NULL DEFAULT 0,
    `next_attempt_at`     DATETIME         NOT NULL,
    `last_error`          TEXT             NULL,
    `provider_message_id` VARCHAR(128)     NULL,
    `sent_at`             DATETIME         NULL,
    `created_at`          DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`          DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP
                                           ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_delivery_due` (`status`, `next_attempt_at`),
    KEY `idx_delivery_car_day` (`car_id`, `count_day`, `count_type`),
    CONSTRAINT `fk_delivery_car`
        FOREIGN KEY (`car_id`) REFERENCES `cars` (`id`)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
