-- =====================================================================
-- Наведение порядка в связях таблиц тарифов.
-- Запускать НА СЕРВЕРЕ (где у пользователей есть доступ к обеим БД).
-- Обе БД должны быть на одном MySQL-сервере и использовать InnoDB.
--
-- Порядок действий:
--   1) выполнить ШАГ 1 (создать новую таблицу car_route_tariffs);
--   2) выполнить миграцию данных: python scripts/migrate_tariff_history.py
--      (перенесёт историю из старой car_tariff_history в новую таблицу);
--   3) выполнить ШАГ 2 и ШАГ 3.
-- =====================================================================

USE PassengersFlowCounting;

-- ---------------------------------------------------------------------
-- ШАГ 1. Нормализованная таблица истории тарифов.
--   car_id  -> cars(id)     ON DELETE CASCADE
--   tariff_id -> tariffs(id) ON DELETE CASCADE
--   UNIQUE(car_id, route_id, start_date) — запрет повторяющихся дат.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS car_route_tariffs (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    car_id     BIGINT UNSIGNED NOT NULL,
    route_id   BIGINT UNSIGNED NOT NULL,
    tariff_id  BIGINT UNSIGNED NOT NULL,
    start_date DATE NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_car_route_date (car_id, route_id, start_date),
    KEY idx_crt_route (route_id),
    KEY idx_crt_tariff (tariff_id),
    CONSTRAINT fk_crt_car    FOREIGN KEY (car_id)    REFERENCES cars(id)    ON DELETE CASCADE,
    CONSTRAINT fk_crt_tariff FOREIGN KEY (tariff_id) REFERENCES tariffs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =====================================================================
-- >>> Здесь выполните миграцию данных (scripts/migrate_tariff_history.py),
--     затем удалите старую таблицу:
-- DROP TABLE IF EXISTS car_tariff_history;
-- =====================================================================


-- ---------------------------------------------------------------------
-- ШАГ 2. Межбазовый FK: tariffs.route_id -> RoutesAndAdvertisement.routes(id)
--
-- Ошибка #1005 / errno 150 «Foreign key constraint is incorrectly formed»
-- почти всегда означает одно из:
--   1) типы колонок не совпадают ТОЧНО (int vs bigint, signed vs unsigned);
--   2) таблица routes не InnoDB (MyISAM не поддерживает FK);
--   3) остались «осиротевшие» route_id в tariffs;
--   4) нет привилегии REFERENCES на routes.
--
-- Сначала выполните диагностику (скопируйте результаты, если FK снова не создастся):
-- ---------------------------------------------------------------------

SHOW COLUMNS FROM RoutesAndAdvertisement.routes LIKE 'id';
SHOW COLUMNS FROM PassengersFlowCounting.tariffs LIKE 'route_id';

SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE
FROM information_schema.TABLES
WHERE (TABLE_SCHEMA = 'RoutesAndAdvertisement' AND TABLE_NAME = 'routes')
   OR (TABLE_SCHEMA = 'PassengersFlowCounting' AND TABLE_NAME = 'tariffs');

-- Осиротевшие тарифы (должно вернуть 0 строк):
SELECT t.id, t.route_id, t.name
FROM PassengersFlowCounting.tariffs t
LEFT JOIN RoutesAndAdvertisement.routes r ON r.id = t.route_id
WHERE r.id IS NULL;

-- ---------------------------------------------------------------------
-- 2a. Привести tariffs.route_id к ТОЧНО такому же типу, как routes.id.
--     Смотрите результат SHOW COLUMNS выше и выберите ОДИН вариант ниже.
-- ---------------------------------------------------------------------

-- Вариант A (самый частый): routes.id = int unsigned
-- ALTER TABLE PassengersFlowCounting.tariffs
--     MODIFY route_id INT UNSIGNED NOT NULL;

-- Вариант B: routes.id = int signed (без UNSIGNED)
-- ALTER TABLE PassengersFlowCounting.tariffs
--     MODIFY route_id INT NOT NULL;

-- Вариант C: routes.id = bigint unsigned (редко)
-- ALTER TABLE PassengersFlowCounting.tariffs
--     MODIFY route_id BIGINT UNSIGNED NOT NULL;

-- ---------------------------------------------------------------------
-- 2b. Убрать осиротевшие тарифы (обязательно перед FK):
-- ---------------------------------------------------------------------
DELETE t FROM PassengersFlowCounting.tariffs t
LEFT JOIN RoutesAndAdvertisement.routes r ON r.id = t.route_id
WHERE r.id IS NULL;

-- ---------------------------------------------------------------------
-- 2c. Индекс на route_id (если его ещё нет — иначе FK тоже может не создаться):
-- ---------------------------------------------------------------------
-- SHOW INDEX FROM PassengersFlowCounting.tariffs WHERE Column_name = 'route_id';
-- ALTER TABLE PassengersFlowCounting.tariffs ADD INDEX idx_tariffs_route_id (route_id);

-- ---------------------------------------------------------------------
-- 2d. Создать FK (после успешного MODIFY route_id):
-- ---------------------------------------------------------------------
ALTER TABLE PassengersFlowCounting.tariffs
    ADD CONSTRAINT fk_tariffs_route
    FOREIGN KEY (route_id) REFERENCES RoutesAndAdvertisement.routes(id)
    ON DELETE CASCADE;


-- ---------------------------------------------------------------------
-- ШАГ 3 (необязательно). Разовая ручная чистка «осиротевших» строк,
--   если что-то осталось от до-FK времён.
-- ---------------------------------------------------------------------
-- Периоды без существующего тарифа:
DELETE crt FROM PassengersFlowCounting.car_route_tariffs crt
LEFT JOIN PassengersFlowCounting.tariffs t ON t.id = crt.tariff_id
WHERE t.id IS NULL;

-- Периоды без существующей машины:
DELETE crt FROM PassengersFlowCounting.car_route_tariffs crt
LEFT JOIN PassengersFlowCounting.cars c ON c.id = crt.car_id
WHERE c.id IS NULL;
