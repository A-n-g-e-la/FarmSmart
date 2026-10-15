def create_farmer_profile(cursor, user_id, location):
    # Link the profile to the new user; MariaDB generates a separate farmer_id.
    cursor.execute(
        "INSERT INTO farmers (user_id, location) VALUES (%s, %s)",
        (user_id, location),
    )
    return cursor.lastrowid
