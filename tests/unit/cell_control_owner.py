"""Provide an explicit owner for a test's already-open raw session."""

from typing import Any

from server.cli.cell_control import Owner


def owner(conn: Any) -> Owner:
    owned = Owner()
    owned.conn = conn
    return owned
