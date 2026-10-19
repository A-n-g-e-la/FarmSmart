-- MANUAL ONLY. This file is not loaded by the application or tests.
-- MariaDB 10.4+. Apply only after separate approval; see README.md.
-- Select the intended existing database in the client before sourcing this file.
-- Stop every plot writer for the entire operation, verify a current restorable
-- backup, and set the three session confirmation variables documented in README.md.
-- Do not use --force. DDL implicitly commits; ROLLBACK cannot undo this migration.
-- This is an EMPTY-TABLE migration: it must never relabel existing hectare values.

DELIMITER //
BEGIN NOT ATOMIC
    IF DATABASE() IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: select the intended database first.';
    END IF;
    IF COALESCE(@farmadvis_acres_migration_approved, 0) <> 1
       OR COALESCE(@farmadvis_backup_verified, 0) <> 1
       OR COALESCE(@farmadvis_plot_writes_paused, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: approval, verified backup and paused writers are required.';
    END IF;
    IF (SELECT COUNT(*) FROM farm_plots) <> 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: farm plots exist; a separate data-conversion migration is required.';
    END IF;
    IF (SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'farm_plots'
          AND COLUMN_NAME = 'area_ha' AND DATA_TYPE = 'decimal'
          AND NUMERIC_PRECISION = 8 AND NUMERIC_SCALE = 3
          AND IS_NULLABLE = 'NO') <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: source area column does not match the inspected schema.';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'farm_plots'
                 AND COLUMN_NAME = 'area_acres') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: target column already exists; inspect migration state.';
    END IF;

    ALTER TABLE farm_plots
        DROP CONSTRAINT chk_farm_plots_area_ha_positive,
        CHANGE COLUMN area_ha area_acres DECIMAL(12,6) NOT NULL,
        ADD CONSTRAINT chk_farm_plots_area_acres_positive CHECK (area_acres > 0);
END//
DELIMITER ;

SHOW CREATE TABLE farm_plots;
SELECT COUNT(*) AS plot_count FROM farm_plots;
