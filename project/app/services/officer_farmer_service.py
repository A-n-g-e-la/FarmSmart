import logging

from app.db import get_db_connection


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
