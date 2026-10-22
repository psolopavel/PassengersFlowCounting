-- Тип підрахунку розкладу: цілоденний (місто/міжмісто, стара логіка) або
-- підрахунок лише на кінцевих зупинках (зони type='final_stop', прив'язані до
-- машини через RoutesAndAdvertisement.all_car_routes.zones_id).
--
-- Обидва типи можуть співіснувати для однієї машини і не заважають один одному:
-- унікальність/конфлікти розкладів рахуються В МЕЖАХ одного type.
ALTER TABLE `schedules`
    ADD COLUMN `type` ENUM('whole_day','final_stops')
        NOT NULL DEFAULT 'whole_day' AFTER `car_id`;
