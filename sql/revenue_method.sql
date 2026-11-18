-- =====================================================================
-- Спосіб розрахунку виручки міжміста (cars.revenue_method).
--   fifo — хто раніше сів — той раніше вийшов (як було досі);
--   lifo — хто пізніше сів — той раніше вийшов; ті, хто сів першими,
--          виходять наприкінці ходки.
-- =====================================================================

USE PassengersFlowCounting;

ALTER TABLE `cars`
    ADD COLUMN IF NOT EXISTS `revenue_method`
        ENUM('fifo','lifo') NOT NULL DEFAULT 'fifo'
        AFTER `count_optimization`;
