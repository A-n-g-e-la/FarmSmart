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
    area_ha DECIMAL(21,14) NOT NULL,
    location VARCHAR(150) NULL,
    soil_type VARCHAR(100) NULL,
    farming_type VARCHAR(100) NULL,
    county VARCHAR(50) NULL,
    sub_county VARCHAR(100) NULL,
    ward VARCHAR(100) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (plot_id),
    INDEX idx_farm_plots_farmer_id (farmer_id),
    CONSTRAINT chk_farm_plots_area_ha_positive CHECK (area_ha > 0),
    CONSTRAINT fk_farm_plots_farmer_id
        FOREIGN KEY (farmer_id) REFERENCES farmers (farmer_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
