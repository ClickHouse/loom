"""The SQL a read of `loom.namespace_grants` uses to find the rows that
confer access, and to put a subject's highest grant first.

The authorization gate (`server/auth`) and the identity plane read the same
table, and a platform package may not import a domain package nor a domain
store a platform one, so the two fragments live here, once.
"""

from __future__ import annotations

# A grant confers access only while unrevoked and active: a `pending` row is
# a join request awaiting an admin (namespace.join on an approval-gated
# namespace) and grants nothing.
LIVE_GRANT = "revoked_at IS NULL AND status = 'active'"

# ORDER BY key (DESC) that puts a subject's highest grant row first; a role
# that is not a grant role ranks below viewer.
GRANT_RANK_SQL = "multiIf(role = 'admin', 3, role = 'writer', 2, role = 'viewer', 1, 0)"
