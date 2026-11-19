-- MANUAL ONLY: not executed by Flask or tests.
-- Apply to the CURRENT backend schema after verifying a restorable backup,
-- pausing writers and confirming area_ha exists and area_acres does not.
-- If administrative columns already exist, inspect them and omit those ADDs.
-- For a legacy acres-only schema, follow docs/farmer_plot_integration.md instead.
-- MySQL DDL implicitly commits. Never run the old area_ha-to-acres migration.

ALTER TABLE farm_plots
    MODIFY COLUMN area_ha DECIMAL(21,14) NOT NULL,
    ADD COLUMN county VARCHAR(50) NULL,
    ADD COLUMN sub_county VARCHAR(100) NULL,
    ADD COLUMN ward VARCHAR(100) NULL;

-- Existing area_ha values retain their units and numeric values.
-- Existing names, optional location/soil/farming text and ownership are unchanged.
-- Administrative fields remain NULL for legacy plots until explicitly updated.
