import logging
import re
from collections.abc import Mapping
from decimal import Decimal

from app.db import get_db_connection


logger = logging.getLogger(__name__)
PLOT_FIELDS = ("plot_name", "area_acres", "location", "soil_type", "farming_type")


def _validate_plot_fields(data, partial=False):
    """Normalize the same field rules for creation and supplied PATCH fields."""
    errors = {}
    values = {}
    # Soil/farming types are VARCHAR fields, not enums, in the existing schema.
    for field, limit in (("plot_name", 100), ("location", 150),
                         ("soil_type", 100), ("farming_type", 100)):
        if partial and field not in data:
            continue
        value = data.get(field)
        if field != "plot_name" and value is None:
            values[field] = None
        elif not isinstance(value, str):
            errors[field] = "Provide text." if value is not None else "This field is required."
        elif len(value.strip()) > limit:
            errors[field] = f"Use at most {limit} characters."
        elif field == "plot_name" and not value.strip():
            errors[field] = "This field is required."
        else:
            values[field] = value.strip() or None

    if not partial or "area_acres" in data:
        raw_area = data.get("area_acres")
        # Use decimal text rather than binary float arithmetic. Reject rounding,
        # exponent strings, non-finite values, booleans, and non-scalar inputs.
        area_text = str(raw_area).strip() if type(raw_area) in (str, int, float, Decimal) else ""
        if (len(area_text) > 32
                or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,3})?", area_text)
                or not Decimal("0.001") <= Decimal(area_text) <= Decimal("999999.999")):
            errors["area_acres"] = "Use acres from 0.001 to 999999.999 with at most 3 decimal places."
        else:
            values["area_acres"] = Decimal(area_text)
    return values, errors


def register_farmer_plot(farmer_id, data):
    """Create a plot owned by a server-authorized farmer, never by payload IDs."""
    if type(farmer_id) is not int or farmer_id <= 0:
        raise ValueError("A validated farmer ID is required.")
    if not isinstance(data, Mapping):
        return {"ok": False, "code": "validation_error", "errors": {
            "request": "Provide a plot object.",
        }}

    values, errors = _validate_plot_fields(data)
    if errors:
        return {"ok": False, "code": "validation_error", "errors": errors}

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        connection.start_transaction()
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO farm_plots "
            "(farmer_id, plot_name, area_acres, location, soil_type, farming_type) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (farmer_id, values["plot_name"], values["area_acres"],
             values["location"], values["soil_type"], values["farming_type"]),
        )
        plot_id = cursor.lastrowid
        connection.commit()
        return {"ok": True, "plot_id": plot_id, "message": "Farm plot registered successfully."}
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                logger.error("Farmer plot registration rollback failed.")
        logger.error("Farmer plot registration failed.")
        return {
            "ok": False, "code": "database_error",
            "message": "Farm plot could not be registered. Try again later.",
        }
    finally:
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Farmer plot registration %s cleanup failed.", label)


def update_farmer_plot(farmer_id, plot_id, data):
    """Update only supplied allowed fields of a plot owned by an authorized farmer."""
    if type(farmer_id) is not int or farmer_id <= 0:
        raise ValueError("A validated farmer ID is required.")
    not_found = {"ok": False, "code": "not_found", "message": "Farm plot not found."}
    if type(plot_id) is not int or not 0 < plot_id <= 18446744073709551615:
        return not_found
    if not isinstance(data, Mapping) or not data:
        return {"ok": False, "code": "validation_error", "errors": {
            "request": "Provide at least one plot field to update.",
        }}
    if any(field not in PLOT_FIELDS for field in data):
        return {"ok": False, "code": "validation_error", "errors": {
            "request": "Only plot_name, area_acres, location, soil_type and farming_type may be updated.",
        }}
    values, errors = _validate_plot_fields(data, partial=True)
    if errors:
        return {"ok": False, "code": "validation_error", "errors": errors}

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        connection.start_transaction()
        cursor = connection.cursor()
        # Lock the owned row so missing/unowned plots and unchanged-value updates
        # are distinguishable without trusting affected-row counts.
        cursor.execute(
            "SELECT plot_id FROM farm_plots WHERE plot_id = %s AND farmer_id = %s FOR UPDATE",
            (plot_id, farmer_id),
        )
        if cursor.fetchone() is None:
            connection.rollback()
            return not_found
        supplied = [field for field in PLOT_FIELDS if field in values]
        # Column identifiers come exclusively from the fixed allowlist above.
        assignments = ", ".join(field + " = %s" for field in supplied)
        cursor.execute(
            "UPDATE farm_plots SET " + assignments + " WHERE plot_id = %s AND farmer_id = %s",
            tuple(values[field] for field in supplied) + (plot_id, farmer_id),
        )
        connection.commit()
        return {"ok": True, "plot_id": plot_id, "message": "Farm plot updated successfully."}
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                logger.error("Farmer plot update rollback failed.")
        logger.error("Farmer plot update failed.")
        return {
            "ok": False, "code": "database_error",
            "message": "Farm plot could not be updated. Try again later.",
        }
    finally:
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Farmer plot update %s cleanup failed.", label)


def get_farmer_plots(farmer_id):
    """List plots for an ID supplied by server-side farmer authorization."""
    if type(farmer_id) is not int or farmer_id <= 0:
        raise ValueError("A validated farmer ID is required.")

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT plot_id, plot_name, area_acres, location, soil_type, farming_type "
            "FROM farm_plots WHERE farmer_id = %s ORDER BY plot_id ASC",
            (farmer_id,),
        )
        # Allowlist response fields and preserve DECIMAL precision as a JSON string.
        plots = [{
            "plot_id": row["plot_id"],
            "plot_name": row["plot_name"],
            "area_acres": str(row["area_acres"]),
            "location": row["location"],
            "soil_type": row["soil_type"],
            "farming_type": row["farming_type"],
        } for row in cursor.fetchall()]
        return {"ok": True, "plots": plots}
    except Exception:
        # Fail closed on unavailable connections or reads; never expose SQL details.
        logger.error("Farmer plot retrieval failed.")
        return {
            "ok": False,
            "code": "database_error",
            "message": "Farm plots could not be retrieved. Try again later.",
        }
    finally:
        # SELECT only: no commits or mutations. Close even after a cleanup failure.
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Farmer plot %s cleanup failed.", label)
