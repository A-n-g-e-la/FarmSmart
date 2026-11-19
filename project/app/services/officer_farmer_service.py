import logging
from decimal import Decimal

from app.db import get_db_connection
from app.models.agricultural_plan import read_products_and_selection
from app.services.feature_preparation_service import ACRE_TO_HA


logger = logging.getLogger(__name__)


def get_assigned_farmers(agricultural_officer_id):
    """List farmers for an officer profile ID supplied by server authorization."""
    if type(agricultural_officer_id) is not int or agricultural_officer_id <= 0:
        raise ValueError("A validated officer profile ID is required.")
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT f.farmer_id, u.first_name, u.last_name, u.phone_number, "
            "COUNT(p.plot_id) AS plot_count FROM farmers AS f "
            "JOIN users AS u ON u.user_id = f.user_id "
            "LEFT JOIN farm_plots AS p ON p.farmer_id = f.farmer_id "
            "WHERE f.assigned_officer_id = %s AND u.role = 'farmer' AND u.status = 'active' "
            "GROUP BY f.farmer_id, u.first_name, u.last_name, u.phone_number "
            "ORDER BY f.farmer_id ASC",
            (agricultural_officer_id,),
        )
        # LEFT JOIN keeps farmers with zero plots. Grouping by farmer ID keeps
        # different farmers distinct, even if names/contact values are identical.
        farmers = [{
            "farmer_id": row["farmer_id"],
            "name": f"{row['first_name']} {row['last_name']}".strip(),
            "phone_number": row["phone_number"],
            "plot_count": int(row["plot_count"]),
        } for row in cursor.fetchall()]
        return {"ok": True, "farmers": farmers}
    except Exception:
        logger.error("Assigned farmer retrieval failed.")
        return {"ok": False, "code": "database_error",
                "message": "Assigned farmers could not be retrieved. Try again later."}
    finally:
        # SELECT only. Closing the connection also ends its read transaction.
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Assigned farmer %s cleanup failed.", label)


def get_assigned_farmer_plots(agricultural_officer_id, farmer_id):
    """Read plots only through the requested farmer's current assignment."""
    if type(agricultural_officer_id) is not int or agricultural_officer_id <= 0:
        raise ValueError("A validated officer profile ID is required.")
    not_found = {"ok": False, "code": "not_found", "message": "Farmer not found."}
    if type(farmer_id) is not int or farmer_id <= 0:
        return not_found
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        # Authorize and retrieve in one SELECT, avoiding an assignment check on
        # one connection followed by an unscoped plot read on another. LEFT JOIN
        # distinguishes an assigned farmer with zero plots from an inaccessible one.
        cursor.execute(
            "SELECT f.farmer_id, p.plot_id, p.plot_name, p.area_ha, "
            "p.location, p.soil_type, p.farming_type, p.county, p.sub_county, p.ward FROM farmers AS f "
            "JOIN users AS u ON u.user_id = f.user_id "
            "LEFT JOIN farm_plots AS p ON p.farmer_id = f.farmer_id "
            "WHERE f.farmer_id = %s AND f.assigned_officer_id = %s "
            "AND u.role = 'farmer' AND u.status = 'active' ORDER BY p.plot_id ASC",
            (farmer_id, agricultural_officer_id),
        )
        rows = cursor.fetchall()
        if not rows:
            return not_found
        # Match farmer plot serialization: allowlisted fields, nullable optional
        # values, and DECIMAL acreage as text to retain all stored precision.
        plots = [{
            "plot_id": row["plot_id"],
            "plot_name": row["plot_name"],
            "area_ha": str(row["area_ha"]),
            "area_acres": str(Decimal(str(row["area_ha"])) / Decimal(str(ACRE_TO_HA))),
            "location": row["location"],
            "soil_type": row["soil_type"],
            "farming_type": row["farming_type"],
            "county": row["county"], "sub_county": row["sub_county"], "ward": row["ward"],
        } for row in rows if row["plot_id"] is not None]
        return {"ok": True, "farmer_id": farmer_id, "plots": plots}
    except Exception:
        logger.error("Assigned farmer plot retrieval failed.")
        return {"ok": False, "code": "database_error",
                "message": "Farm plots could not be retrieved. Try again later."}
    finally:
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Assigned farmer plot %s cleanup failed.", label)


def get_assigned_farmer_plans(agricultural_officer_id, farmer_id, plot_id=None, plan_id=None):
    """Read existing plans only through a current assignment; never mutate plans."""
    if type(agricultural_officer_id) is not int or agricultural_officer_id <= 0:
        raise ValueError("A validated officer profile ID is required.")
    not_found = {"ok": False, "code": "not_found", "message": "Farmer, plot or plan not found."}
    if type(farmer_id) is not int or not 0 < farmer_id <= 18446744073709551615:
        return not_found
    if any(value is not None and (type(value) is not int or not 0 < value <= 18446744073709551615)
           for value in (plot_id, plan_id)):
        return not_found
    connection = cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        # Authorization and plan retrieval share a single assignment-scoped query.
        # LEFT JOIN retains accessible farmers/plots with no plans as an empty list.
        query = ("SELECT a.*, p.area_ha FROM farmers f JOIN users u ON u.user_id=f.user_id "
                 "LEFT JOIN farm_plots p ON p.farmer_id=f.farmer_id "
                 "LEFT JOIN agricultural_plans a ON a.plot_id=p.plot_id "
                 "WHERE f.farmer_id=%s AND f.assigned_officer_id=%s "
                 "AND u.role='farmer' AND u.status='active'")
        params = (farmer_id, agricultural_officer_id)
        if plot_id is not None:
            query += " AND p.plot_id=%s"
            params += (plot_id,)
        if plan_id is not None:
            query += " AND a.plan_id=%s"
            params += (plan_id,)
        cursor.execute(query + " ORDER BY a.planting_year DESC, a.planting_season, a.plan_id", params)
        rows = cursor.fetchall()
        if not rows:
            return not_found
        plans = [read_products_and_selection(cursor, row) for row in rows if row["plan_id"] is not None]
        if plan_id is not None:
            return {"ok": True, "farmer_id": farmer_id, "plan": plans[0]}
        return {"ok": True, "farmer_id": farmer_id, "plans": plans}
    except Exception:
        logger.error("Assigned farmer plan retrieval failed.")
        return {"ok": False, "code": "database_error", "message": "Plans could not be retrieved. Try again later."}
    finally:
        for resource in (cursor, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Assigned farmer plan cleanup failed.")
