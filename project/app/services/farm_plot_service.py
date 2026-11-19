import logging
import re
from collections.abc import Mapping
from decimal import Decimal

from app.db import get_db_connection
from app.config.plot_options import PLOT_OPTIONS
from app.services.feature_preparation_service import ACRE_TO_HA


logger = logging.getLogger(__name__)
PLOT_FIELDS = ("plot_name", "area_ha", "area_acres", "location", "soil_type", "farming_type", "county", "sub_county", "ward")


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

    for field, options in (("soil_type", PLOT_OPTIONS["soil_types"]),
                           ("farming_type", PLOT_OPTIONS["farming_types"])):
        if field in values and values[field] is not None and values[field] not in options:
            errors[field] = "Select a listed option or Unknown."

    administrative = {"county", "sub_county", "ward"}
    if not partial or administrative.intersection(data):
        if partial and not administrative.issubset(data):
            errors["ward"] = "Submit county, sub-county and ward together when changing administrative location."
        county = data.get("county", PLOT_OPTIONS["county"] if not partial else None)
        if county != PLOT_OPTIONS["county"]:
            errors["county"] = "This prototype supports Uasin Gishu only."
        sub_county, ward = data.get("sub_county"), data.get("ward")
        wards = PLOT_OPTIONS["sub_counties"].get(sub_county) if isinstance(sub_county, str) else None
        if wards is None:
            errors["sub_county"] = "Select a listed Uasin Gishu sub-county."
        if not isinstance(ward, str) or wards is None or ward not in wards:
            errors["ward"] = "Select a ward belonging to the selected sub-county."
        values.update(county=PLOT_OPTIONS["county"], sub_county=sub_county, ward=ward)

    area_keys = [key for key in ("area_ha", "area_acres") if key in data]
    if not partial or area_keys:
        if len(area_keys) != 1:
            errors["area_ha"] = "Supply exactly one of area_ha or area_acres."
        else:
            key = area_keys[0]
            raw = data[key]
            text = str(raw).strip() if type(raw) in (str, int, float, Decimal) else ""
            places = 3 if key == "area_acres" else 14
            if len(text) > 40 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1," + str(places) + r"})?", text):
                errors[key] = f"Use a positive decimal with at most {places} decimal places."
            else:
                hectares = Decimal(text)
                if key == "area_acres":
                    hectares *= Decimal(str(ACRE_TO_HA))
                if not Decimal("0.0001") <= hectares <= Decimal("10000"):
                    errors[key] = "Area must convert to between 0.0001 and 10000 hectares."
                else:
                    values["area_ha"] = hectares
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
            "(farmer_id, plot_name, area_ha, location, soil_type, farming_type, county, sub_county, ward) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (farmer_id, values["plot_name"], values["area_ha"],
             values["location"], values["soil_type"], values["farming_type"],
             values["county"], values["sub_county"], values["ward"]),
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
            "request": "Only editable plot fields may be updated.",
        }}
    values, errors = _validate_plot_fields(data, partial=True)
    if errors:
        return {"ok": False, "code": "validation_error", "errors": errors}

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        connection.start_transaction()
        cursor = connection.cursor(dictionary=True)
        # Lock the owned row so missing/unowned plots and unchanged-value updates
        # are distinguishable without trusting affected-row counts.
        cursor.execute(
            "SELECT plot_id, area_ha FROM farm_plots WHERE plot_id = %s AND farmer_id = %s FOR UPDATE",
            (plot_id, farmer_id),
        )
        owned = cursor.fetchone()
        if owned is None:
            connection.rollback()
            return not_found
        supplied = [field for field in PLOT_FIELDS if field in values]
        area_changed = "area_ha" in values and values["area_ha"] != Decimal(str(owned["area_ha"]))
        # Column identifiers come exclusively from the fixed allowlist above.
        assignments = ", ".join(field + " = %s" for field in supplied)
        cursor.execute(
            "UPDATE farm_plots SET " + assignments + " WHERE plot_id = %s AND farmer_id = %s",
            tuple(values[field] for field in supplied) + (plot_id, farmer_id),
        )
        if area_changed:
            cursor.execute("DELETE FROM agricultural_plan_selections WHERE plan_id IN "
                           "(SELECT plan_id FROM agricultural_plans WHERE plot_id = %s)", (plot_id,))
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


def get_farmer_plots(farmer_id, plot_id=None):
    """List plots for an ID supplied by server-side farmer authorization."""
    if type(farmer_id) is not int or farmer_id <= 0:
        raise ValueError("A validated farmer ID is required.")

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        query = ("SELECT plot_id, plot_name, area_ha, location, soil_type, farming_type, county, sub_county, ward "
                 "FROM farm_plots WHERE farmer_id = %s")
        params = (farmer_id,)
        if plot_id is not None:
            query += " AND plot_id = %s"
            params += (plot_id,)
        cursor.execute(query + " ORDER BY plot_id ASC", params)
        # Acreage is derived for display, never stored independently.
        plots = [{
            "plot_id": row["plot_id"], "plot_name": row["plot_name"],
            "area_ha": str(row["area_ha"]),
            "area_acres": str(Decimal(str(row["area_ha"])) / Decimal(str(ACRE_TO_HA))),
            "location": row["location"], "soil_type": row["soil_type"], "farming_type": row["farming_type"],
            "county": row["county"], "sub_county": row["sub_county"], "ward": row["ward"],
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


def get_farmer_plot(farmer_id, plot_id):
    """Read one owned plot without exposing whether another farmer owns it."""
    if type(plot_id) is not int or not 0 < plot_id <= 18446744073709551615:
        return {"ok": False, "code": "not_found", "message": "Farm plot not found."}
    result = get_farmer_plots(farmer_id, plot_id)
    if not result["ok"]:
        return result
    if not result["plots"]:
        return {"ok": False, "code": "not_found", "message": "Farm plot not found."}
    return {"ok": True, "plot": result["plots"][0]}


def delete_farmer_plot(farmer_id, plot_id):
    """Delete an owned plot only when no agricultural plan references it."""
    if type(farmer_id) is not int or farmer_id <= 0:
        raise ValueError("A validated farmer ID is required.")
    not_found = {"ok": False, "code": "not_found", "message": "Farm plot not found."}
    if type(plot_id) is not int or not 0 < plot_id <= 18446744073709551615:
        return not_found
    connection = cursor = None
    try:
        connection = get_db_connection()
        connection.start_transaction()
        cursor = connection.cursor()
        cursor.execute("SELECT plot_id FROM farm_plots WHERE plot_id=%s AND farmer_id=%s FOR UPDATE", (plot_id, farmer_id))
        if cursor.fetchone() is None:
            connection.rollback()
            return not_found
        cursor.execute("SELECT plan_id FROM agricultural_plans WHERE plot_id=%s LIMIT 1", (plot_id,))
        if cursor.fetchone() is not None:
            connection.rollback()
            return {"ok": False, "code": "plot_has_plans", "message": "Remove this plot's plans before deleting the plot."}
        cursor.execute("DELETE FROM farm_plots WHERE plot_id=%s AND farmer_id=%s", (plot_id, farmer_id))
        connection.commit()
        return {"ok": True, "plot_id": plot_id, "message": "Farm plot deleted successfully."}
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                logger.error("Farmer plot deletion rollback failed.")
        logger.error("Farmer plot deletion failed.")
        return {"ok": False, "code": "database_error", "message": "Farm plot could not be deleted. Try again later."}
    finally:
        for resource in (cursor, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Farmer plot deletion cleanup failed.")
