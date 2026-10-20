def create_agricultural_officer_profile(cursor, user_id, region, organization):
    cursor.execute(
        "INSERT INTO agricultural_officers "
        "(user_id, region_assigned, organization, approved_at) VALUES (%s, %s, %s, NULL)",
        (user_id, region, organization),
    )
    return cursor.lastrowid
