import logging
import re

import mysql.connector
from flask import current_app


def get_db_connection():
    connection = mysql.connector.connect(
        host=current_app.config["MYSQL_HOST"],
        port=current_app.config["MYSQL_PORT"],
        user=current_app.config["MYSQL_USER"],
        password=current_app.config["MYSQL_PASSWORD"],
        database=current_app.config["MYSQL_DATABASE"]
    )

    return connection


class UssdDatabaseError(Exception):
    """Safe operational failure; contains no configuration or SQL diagnostics."""


def _ussd_timeout(name):
    value = current_app.config.get(name)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]?", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 30:
        raise UssdDatabaseError("USSD database connection is unavailable.")
    return value


def get_ussd_db_connection():
    """Initialize finite, session-only waits before returning to a transaction.

    The general web helper is unchanged. There are no reconnects or retries.
    Socket/statement limits are not a hard overall authentication deadline.
    """
    connection = cursor = None
    try:
        timeouts = {name: _ussd_timeout("USSD_DB_" + name + "_TIMEOUT") for name in
                    ("CONNECTION", "READ", "WRITE", "LOCK_WAIT", "STATEMENT")}
        connection = mysql.connector.connect(
            host=current_app.config["MYSQL_HOST"],
            port=current_app.config["MYSQL_PORT"],
            user=current_app.config["MYSQL_USER"],
            password=current_app.config["MYSQL_PASSWORD"],
            database=current_app.config["MYSQL_DATABASE"],
            connection_timeout=timeouts["CONNECTION"],
            read_timeout=timeouts["READ"],
            write_timeout=timeouts["WRITE"],
        )
        cursor = connection.cursor()
        cursor.execute("SET SESSION innodb_lock_wait_timeout = %s", (timeouts["LOCK_WAIT"],))
        cursor.execute("SET SESSION max_statement_time = %s", (timeouts["STATEMENT"],))
        cursor.close()
        cursor = None
        return connection
    except Exception:
        # Initialization can fail before ownership reaches ussd_transaction().
        # Attempt each cleanup independently; never log the original exception.
        cleanup = []
        if connection is not None:
            cleanup.append(("rollback", connection.rollback))
        if cursor is not None:
            cleanup.append(("cursor", cursor.close))
        if connection is not None:
            cleanup.append(("connection", connection.close))
        for label, operation in cleanup:
            try:
                operation()
            except Exception:
                logging.getLogger(__name__).warning("USSD initialization %s cleanup failed.", label)
        raise UssdDatabaseError("USSD database connection is unavailable.") from None
