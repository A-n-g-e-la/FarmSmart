def phone_number_exists(cursor, phone_number):
    # Bind the normalized number as a parameter rather than interpolating SQL.
    cursor.execute(
        "SELECT user_id FROM users WHERE phone_number = %s LIMIT 1",
        (phone_number,),
    )
    return cursor.fetchone() is not None


def create_farmer_user(cursor, first_name, last_name, phone_number, password_hash):
    # Set the role and active status internally, never from request fields.
    cursor.execute(
        "INSERT INTO users "
        "(first_name, last_name, phone_number, password_hash, role, status) "
        "VALUES (%s, %s, %s, %s, 'farmer', 'active')",
        (first_name, last_name, phone_number, password_hash),
    )
    return cursor.lastrowid
