SET SESSION time_zone = '+00:00';

CREATE TABLE users (
    user_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    phone_number VARCHAR(20) NULL,
    email VARCHAR(254) NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('farmer', 'officer') NOT NULL,
    -- Safe fallback: registration explicitly activates farmers, never officers.
    status ENUM('pending', 'active', 'disabled') NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    CONSTRAINT uq_users_phone_number UNIQUE (phone_number),
    CONSTRAINT uq_users_email UNIQUE (email)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE agricultural_officers (
    agricultural_officer_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    region_assigned VARCHAR(100) NULL,
    organization VARCHAR(150) NULL,
    approved_at DATETIME NULL,
    PRIMARY KEY (agricultural_officer_id),
    CONSTRAINT uq_agricultural_officers_user_id UNIQUE (user_id),
    CONSTRAINT fk_agricultural_officers_user_id
        FOREIGN KEY (user_id) REFERENCES users (user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE farmers (
    farmer_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    location VARCHAR(150) NULL,
    assigned_officer_id BIGINT UNSIGNED NULL,
    PRIMARY KEY (farmer_id),
    CONSTRAINT uq_farmers_user_id UNIQUE (user_id),
    INDEX idx_farmers_assigned_officer_id (assigned_officer_id),
    CONSTRAINT fk_farmers_user_id
        FOREIGN KEY (user_id) REFERENCES users (user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_farmers_assigned_officer_id
        FOREIGN KEY (assigned_officer_id)
        REFERENCES agricultural_officers (agricultural_officer_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE farm_plots (
    plot_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    farmer_id BIGINT UNSIGNED NOT NULL,
    plot_name VARCHAR(100) NOT NULL,
    area_acres DECIMAL(12,6) NOT NULL,
    location VARCHAR(150) NULL,
    soil_type VARCHAR(100) NULL,
    farming_type VARCHAR(100) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (plot_id),
    INDEX idx_farm_plots_farmer_id (farmer_id),
    CONSTRAINT chk_farm_plots_area_acres_positive CHECK (area_acres > 0),
    CONSTRAINT fk_farm_plots_farmer_id
        FOREIGN KEY (farmer_id) REFERENCES farmers (farmer_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- USSD authentication storage; no existing farmer backfill.
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
