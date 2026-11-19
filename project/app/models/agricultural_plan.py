"""Parameterized SQL for seasonal plans, product snapshots and farmer choices."""

import json


PLAN_COLUMNS = ("planting_year", "planting_season", "variety", "farmgate_price_kes_kg",
                "seed_cost_kes", "labour_cost_kes", "other_cost_kes")


def get_plot(cursor, farmer_id, plot_id):
    cursor.execute("SELECT plot_id, farmer_id, area_ha FROM farm_plots "
                   "WHERE plot_id = %s AND farmer_id = %s FOR UPDATE", (plot_id, farmer_id))
    return cursor.fetchone()


def find_plan(cursor, farmer_id, plan_id):
    # Lock the plan and plot together so edits and selections cannot race.
    cursor.execute("SELECT a.*, p.area_ha FROM agricultural_plans a "
                   "JOIN farm_plots p ON p.plot_id = a.plot_id "
                   "WHERE a.plan_id = %s AND p.farmer_id = %s FOR UPDATE", (plan_id, farmer_id))
    row = cursor.fetchone()
    return read_products_and_selection(cursor, row) if row else None


def find_season(cursor, plot_id, year, season):
    cursor.execute("SELECT plan_id FROM agricultural_plans "
                   "WHERE plot_id = %s AND planting_year = %s AND planting_season = %s",
                   (plot_id, year, season))
    return cursor.fetchone()


def list_plan_ids(cursor, plot_id):
    cursor.execute("SELECT plan_id FROM agricultural_plans WHERE plot_id = %s "
                   "ORDER BY planting_year DESC, planting_season, plan_id", (plot_id,))
    return [row["plan_id"] for row in cursor.fetchall()]


def insert_plan(cursor, plot_id, values):
    cursor.execute("INSERT INTO agricultural_plans "
                   "(plot_id, planting_year, planting_season, variety, farmgate_price_kes_kg, "
                   "seed_cost_kes, labour_cost_kes, other_cost_kes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                   (plot_id, *(values[name] for name in PLAN_COLUMNS)))
    return cursor.lastrowid


def update_plan(cursor, plan_id, values):
    cursor.execute("UPDATE agricultural_plans SET variety=%s, farmgate_price_kes_kg=%s, "
                   "seed_cost_kes=%s, labour_cost_kes=%s, other_cost_kes=%s, "
                   "updated_at=CURRENT_TIMESTAMP WHERE plan_id=%s",
                   (*(values[name] for name in PLAN_COLUMNS[2:]), plan_id))


def replace_products(cursor, plan_id, products):
    cursor.execute("DELETE FROM agricultural_plan_products WHERE plan_id=%s", (plan_id,))
    for position, product in enumerate(products):
        grade = product["grade"]
        cursor.execute("INSERT INTO agricultural_plan_products "
                       "(plan_id, position, product_name, nitrogen_pct, phosphorus_p2o5_pct, "
                       "potassium_k2o_pct, quantity_kg, pricing_json, cost_kes) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (plan_id, position, product["product"], grade["N"], grade["P2O5"],
                        grade["K2O"], product["quantity_kg"], json.dumps(product["pricing"]), product["cost_kes"]))


def clear_selection(cursor, plan_id):
    cursor.execute("DELETE FROM agricultural_plan_selections WHERE plan_id=%s", (plan_id,))


def save_selection(cursor, plan_id, scenario):
    # Replacing the single choice is atomic inside the service transaction.
    clear_selection(cursor, plan_id)
    cursor.execute("INSERT INTO agricultural_plan_selections "
                   "(plan_id, scenario_id, scaling_percentage, products_json) VALUES (%s,%s,%s,%s)",
                   (plan_id, scenario["scenario_id"], scenario["scaling_percentage"],
                    json.dumps(scenario["fertilizer"]["products"])))


def delete_plan(cursor, plan_id):
    # Only the new child tables cascade. The plot and farmer remain intact.
    cursor.execute("DELETE FROM agricultural_plans WHERE plan_id=%s", (plan_id,))


def read_products_and_selection(cursor, row):
    plan = dict(row)
    plan["area_ha"] = float(plan["area_ha"])
    for key in ("farmgate_price_kes_kg", "seed_cost_kes", "labour_cost_kes", "other_cost_kes"):
        plan[key] = float(plan[key])
    for key in ("created_at", "updated_at"):
        value = plan[key]
        plan[key] = value.isoformat() if hasattr(value, "isoformat") else str(value)
    cursor.execute("SELECT * FROM agricultural_plan_products WHERE plan_id=%s ORDER BY position", (plan["plan_id"],))
    plan["fertilizers"] = [{
        "product": p["product_name"], "quantity_kg": float(p["quantity_kg"]),
        "grade": {"N": float(p["nitrogen_pct"]), "P2O5": float(p["phosphorus_p2o5_pct"]),
                  "K2O": float(p["potassium_k2o_pct"])},
        "pricing": json.loads(p["pricing_json"]), "cost_kes": float(p["cost_kes"]),
    } for p in cursor.fetchall()]
    cursor.execute("SELECT * FROM agricultural_plan_selections WHERE plan_id=%s", (plan["plan_id"],))
    selection = cursor.fetchone()
    plan["selection"] = None if selection is None else {
        "plan_id": selection["plan_id"], "scenario_id": selection["scenario_id"],
        "scaling_percentage": selection["scaling_percentage"],
        "fertilizer_products": json.loads(selection["products_json"]),
        "selected_at": (selection["selected_at"].isoformat() if hasattr(selection["selected_at"], "isoformat")
                        else str(selection["selected_at"])),
    }
    return plan
