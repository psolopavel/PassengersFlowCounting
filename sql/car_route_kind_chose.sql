-- Дозволити рахувальнику самому обирати тип маршруту (місто / міжмісто) у панелі.
ALTER TABLE `cars`
    ADD COLUMN `route_kind_chose` TINYINT(1) NOT NULL DEFAULT 0 AFTER `route_kind`;
