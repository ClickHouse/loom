"""An error as it may be stored or logged where its text could quote a caller's
content: its type and a code, never its message."""

from __future__ import annotations

from clickhouse_connect.driver.exceptions import DatabaseError

from server.core.ch_errors import error_code


def error_summary(e: BaseException) -> str:
    """An error as it is stored or logged where its text could quote a caller's content
    (a parse error, a provider's 4xx body): its type, and a ClickHouse code, an HTTP
    status or a Postgres SQLSTATE when it has one."""
    name = type(e).__name__
    code = error_code(e) if isinstance(e, DatabaseError) else None
    if code is not None:
        return f"{name} code {code}"
    status = getattr(e, "status_code", None)
    if status:
        name = f"{name} status {status}"
    # A Postgres error's SQLSTATE names what failed (a deadlock, a timeout) and holds no text.
    for err in (e, e.__cause__):
        sqlstate = getattr(err, "sqlstate", None)
        if isinstance(sqlstate, str):
            return f"{name} sqlstate {sqlstate}"
    return name
