"""The SQL a read of `loom.namespace_grants` uses to find the rows that
confer access, and to put a subject's highest grant first.

The authorization gate (`server/auth`) and the identity plane read the same
table, and a platform package may not import a domain package nor a domain
store a platform one, so the two fragments live here, once.
"""

from __future__ import annotations

from server.core.principals import EVERYONE, ONE_SPELLING

# A grant confers access only while unrevoked and active: a `pending` row is
# a join request awaiting an admin (namespace.join on an approval-gated
# namespace) and grants nothing.
LIVE_GRANT = "revoked_at IS NULL AND status = 'active'"


def grant_rank_sql(column: str) -> str:
    """ORDER BY key (DESC) that puts the highest grant role in `column` first; a
    value that is not a grant role ranks below viewer."""
    return f"multiIf({column} = 'admin', 3, {column} = 'writer', 2, {column} = 'viewer', 1, 0)"


# ORDER BY key (DESC) that puts a subject's highest grant row first.
GRANT_RANK_SQL = grant_rank_sql("role")


def has_aliases_sql(column: str) -> str:
    """`server.core.principals.has_aliases` over the subject column `column`:
    whether the directory may tie it to a person's other spellings."""
    one_spelling = [f"NOT startsWith({column}, '{prefix}')" for prefix in ONE_SPELLING]
    return " AND ".join([f"{column} NOT IN ('', '*', '{EVERYONE}')", *one_spelling])


# Whether a `loom.namespaces` row's `contact_email` is its owner's own address, so
# another spelling of them: an SSO account's (it has no password; the identity provider
# verified the email) or one linked to the address (`loom.sso_links`). Self-signup never
# verifies the email it is given, and whoever typed an address first must not hold the
# grants written under it.
VERIFIED_EMAIL_SQL = (
    "(password_hash = '' OR (org, namespace, contact_email) IN ("
    "SELECT org, namespace, email FROM loom.sso_links FINAL WHERE linked = 1))"
)
