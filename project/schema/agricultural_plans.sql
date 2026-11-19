-- Manual additive schema change. Review before applying to the existing database.
-- Requires the backend farm_plots table (plot_id BIGINT UNSIGNED, area_ha).
-- No existing tables are altered, dropped or reset.

CREATE TABLE agricultural_plans (
    plan_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    plot_id BIGINT UNSIGNED NOT NULL,
    planting_year SMALLINT UNSIGNED NOT NULL,
    planting_season VARCHAR(20) NOT NULL,
    variety VARCHAR(4) NOT NULL,
    farmgate_price_kes_kg DOUBLE NOT NULL,
    seed_cost_kes DOUBLE NOT NULL,
    labour_cost_kes DOUBLE NOT NULL,
    other_cost_kes DOUBLE NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (plan_id),
    CONSTRAINT uq_agricultural_plans_season UNIQUE (plot_id, planting_year, planting_season),
    CONSTRAINT fk_agricultural_plans_plot FOREIGN KEY (plot_id) REFERENCES farm_plots (plot_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_agricultural_plan_costs CHECK (farmgate_price_kes_kg >= 0
        AND seed_cost_kes >= 0 AND labour_cost_kes >= 0 AND other_cost_kes >= 0)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE agricultural_plan_products (
    plan_product_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    plan_id BIGINT UNSIGNED NOT NULL,
    position SMALLINT UNSIGNED NOT NULL,
    product_name VARCHAR(100) NOT NULL,
    nitrogen_pct DOUBLE NOT NULL,
    phosphorus_p2o5_pct DOUBLE NOT NULL,
    potassium_k2o_pct DOUBLE NOT NULL,
    quantity_kg DOUBLE NOT NULL,
    pricing_json JSON NOT NULL,
    cost_kes DOUBLE NOT NULL,
    PRIMARY KEY (plan_product_id),
    CONSTRAINT uq_agricultural_plan_product_position UNIQUE (plan_id, position),
    CONSTRAINT fk_agricultural_plan_products_plan FOREIGN KEY (plan_id)
        REFERENCES agricultural_plans (plan_id) ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT chk_agricultural_plan_product_values CHECK (quantity_kg >= 0 AND cost_kes >= 0
        AND nitrogen_pct >= 0 AND phosphorus_p2o5_pct >= 0 AND potassium_k2o_pct >= 0
        AND nitrogen_pct + phosphorus_p2o5_pct + potassium_k2o_pct <= 100)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE agricultural_plan_selections (
    plan_id BIGINT UNSIGNED NOT NULL,
    scenario_id VARCHAR(32) NOT NULL,
    scaling_percentage SMALLINT UNSIGNED NOT NULL,
    products_json JSON NOT NULL,
    selected_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plan_id),
    CONSTRAINT fk_agricultural_plan_selection_plan FOREIGN KEY (plan_id)
        REFERENCES agricultural_plans (plan_id) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
