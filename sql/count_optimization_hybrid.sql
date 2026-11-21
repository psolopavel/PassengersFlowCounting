-- =====================================================================
-- Гібридний підрахунок міжміста (cars.count_optimization = 'hybrid'):
-- відрізки за треком там, де він є (без дорисовки), а розриви треку та
-- з'їзди з маршруту рахувальник розмічає вручну хронологією.
-- =====================================================================

USE PassengersFlowCounting;

ALTER TABLE `cars`
    MODIFY COLUMN `count_optimization`
        ENUM('none','auto_gap_filling','manual_chronology','hybrid') NOT NULL DEFAULT 'none';
