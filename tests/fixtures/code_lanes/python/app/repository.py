"""Synthetic application module: DB-API and SQLAlchemy clients."""

from sqlalchemy import text

INSERT_ORDER = "INSERT INTO orders (id, customer_id, total) VALUES (%s, %s, %s)"


def load_customer(cursor, customer_id):
    cursor.execute("SELECT id, name FROM customers WHERE id = %s", (customer_id,))
    return cursor.fetchone()


def recent_orders(session, days):
    return session.execute(
        text("SELECT id FROM orders WHERE created_at > now() - interval :days day"),
        {"days": days},
    )


def write_order(cursor, row):
    cursor.execute(INSERT_ORDER, row)
    cursor.executemany("UPDATE customers SET last_order = ? WHERE id = ?", [row])
