-- MANUAL ONLY. NOT EXECUTED during Stage 2B. MariaDB 10.4+.
-- Read README.md first. Obtain separate execution approval and a verified backup.
-- Select the intended database; pause USSD writers; do not use --force.
-- DDL implicitly commits: this three-table migration is NOT atomic.
-- No existing application records are updated, deleted, or backfilled.

DELIMITER //
BEGIN NOT ATOMIC
    IF DATABASE() IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: select the intended database first.';
    END IF;
    IF COALESCE(@farmadvis_ussd_migration_approved, 0) <> 1
       OR COALESCE(@farmadvis_backup_verified, 0) <> 1
       OR COALESCE(@farmadvis_ussd_writes_paused, 0) <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: approval, backup and paused USSD writers are required.';
    END IF;
    IF (SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'farmers'
          AND COLUMN_NAME = 'farmer_id' AND DATA_TYPE = 'bigint'
          AND COLUMN_TYPE LIKE '%unsigned%' AND IS_NULLABLE = 'NO') <> 1
       OR (SELECT COUNT(*) FROM information_schema.TABLES
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'farmers' AND ENGINE = 'InnoDB') <> 1
       OR (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'farmers' AND INDEX_NAME = 'PRIMARY') <> 1
       OR (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'farmers'
             AND INDEX_NAME = 'PRIMARY' AND COLUMN_NAME = 'farmer_id') <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: farmer primary key or storage engine is incompatible.';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.TABLES
               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN
                   ('farmer_ussd_credentials', 'ussd_sessions', 'ussd_callback_results')) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'STOP: USSD tables already exist; inspect partial migration state.';
    END IF;
END//
DELIMITER ;

-- USSD TABLE DEFINITIONS: kept identical to the canonical fresh-install schema.
CREATE TABLE farmer_ussd_credentials (
    farmer_id BIGINT UNSIGNED NOT NULL,
    pin_hash VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NULL,
    pin_key_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    pin_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
    failed_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    locked_until DATETIME(6) NULL,
    web_failed_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    web_locked_until DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (farmer_id),
    CONSTRAINT fk_ussd_credentials_farmer FOREIGN KEY (farmer_id)
        REFERENCES farmers (farmer_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_ussd_pin_key_version CHECK (pin_key_version > 0),
    CONSTRAINT chk_ussd_pin_enrollment CHECK (
        (pin_hash IS NULL AND pin_version = 0) OR (pin_hash IS NOT NULL AND pin_version > 0)),
    CONSTRAINT chk_ussd_pin_failures CHECK (failed_attempts <= 3),
    CONSTRAINT chk_ussd_web_failures CHECK (web_failed_attempts <= 5)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE ussd_sessions (
    session_key BINARY(32) NOT NULL,
    phone_tag BINARY(32) NOT NULL,
    service_code VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    farmer_id BIGINT UNSIGNED NULL,
    state ENUM('welcome', 'pin', 'verified', 'ended') NOT NULL DEFAULT 'welcome',
    verified_pin_version BIGINT UNSIGNED NULL,
    authenticated_at DATETIME(6) NULL,
    -- -1 means no accepted callback yet; position 0 is the empty welcome input.
    last_input_count SMALLINT NOT NULL DEFAULT -1,
    created_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    retain_until DATETIME(6) NOT NULL,
    PRIMARY KEY (session_key),
    INDEX idx_ussd_sessions_farmer (farmer_id),
    INDEX idx_ussd_sessions_expiry (expires_at),
    INDEX idx_ussd_sessions_retention (retain_until, session_key),
    CONSTRAINT fk_ussd_sessions_farmer FOREIGN KEY (farmer_id)
        REFERENCES farmers (farmer_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_ussd_session_expiry CHECK (expires_at > created_at),
    CONSTRAINT chk_ussd_session_retention CHECK (retain_until >= expires_at),
    CONSTRAINT chk_ussd_session_position CHECK (last_input_count BETWEEN -1 AND 16),
    CONSTRAINT chk_ussd_verified_metadata CHECK (
        (state = 'verified' AND farmer_id IS NOT NULL AND verified_pin_version IS NOT NULL
         AND verified_pin_version > 0 AND authenticated_at IS NOT NULL)
        OR (state <> 'verified' AND verified_pin_version IS NULL AND authenticated_at IS NULL))
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE ussd_callback_results (
    session_key BINARY(32) NOT NULL,
    input_count SMALLINT UNSIGNED NOT NULL,
    input_fingerprint BINARY(32) NOT NULL,
    response_text VARCHAR(512) NOT NULL,
    response_status SMALLINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (session_key, input_count),
    CONSTRAINT fk_ussd_callback_session FOREIGN KEY (session_key)
        REFERENCES ussd_sessions (session_key) ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT chk_ussd_callback_position CHECK (input_count <= 16),
    CONSTRAINT chk_ussd_callback_status CHECK (response_status IN (200, 400, 403, 413, 415, 503)),
    CONSTRAINT chk_ussd_callback_prefix CHECK (
        LEFT(response_text, 4) = 'CON ' OR LEFT(response_text, 4) = 'END ')
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
