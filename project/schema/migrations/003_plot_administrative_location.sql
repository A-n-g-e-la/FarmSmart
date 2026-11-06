-- MANUAL ONLY. PREPARED, NOT EXECUTED. MariaDB 10.4+.
-- Obtain separate execution approval, verify the intended database and a current
-- restorable backup, and pause plot writers. DDL implicitly commits.
-- Existing records and location/soil/farming text are NOT backfilled or changed.
DELIMITER //
BEGIN NOT ATOMIC
    IF DATABASE() IS NULL
       OR COALESCE(@farmadvis_plot_location_approved, 0) <> 1
       OR COALESCE(@farmadvis_backup_verified, 0) <> 1
       OR COALESCE(@farmadvis_plot_writes_paused, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: database, approval, backup and paused writers required.';
    END IF;
    IF (SELECT COUNT(*) FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'farm_plots' AND ENGINE = 'InnoDB') <> 1
       OR (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'farm_plots'
             AND COLUMN_NAME = 'area_acres' AND DATA_TYPE = 'decimal'
             AND NUMERIC_PRECISION = 12 AND NUMERIC_SCALE = 6) <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: expected acreage schema not found.';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'farm_plots'
                 AND COLUMN_NAME IN ('county', 'sub_county', 'ward')) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: administrative columns already exist; inspect migration state.';
    END IF;
    ALTER TABLE farm_plots
        ADD COLUMN county VARCHAR(50) NULL AFTER farming_type,
        ADD COLUMN sub_county VARCHAR(100) NULL AFTER county,
        ADD COLUMN ward VARCHAR(100) NULL AFTER sub_county;
END//
DELIMITER ;
