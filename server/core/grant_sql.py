"""The SQL fragments the grant reads share: the rows of `loom.namespace_grants`
that confer access, a subject's highest grant first, the subjects the
directory may tie to a person, and the account emails that tie.

The authorization gate (`server/auth`) and the identity plane read the same
tables, and a platform package may not import a domain package nor a domain
store a platform one, so the fragments live here, once.
"""

from __future__ import annotations

from server.core.principals import EVERYONE, ONE_SPELLING

# A grant confers access only while unrevoked and active: a `pending` row is
# a join request awaiting an admin (namespace.join on an approval-gated
# namespace) and grants nothing.
LIVE_GRANT = "revoked_at IS NULL AND status = 'active'"

# The `sa:` subjects of the tokens in `{org:String}` whose `loom.service_accounts` row is
# revoked or past its expiry, lower-cased. Such a token's own grant confers nothing at the
# gate: the token is refused at verify, and a trusted caller acting for its subject must
# not reach what it held. A subject no row lists (an operator's `loom mint-token`) is not
# here: it ends by a revocation of the subject.
DEAD_TOKEN_SUBJECTS = (
    "SELECT lower(sa_subject) FROM loom.service_accounts FINAL WHERE org = {org:String}"
    " AND (revoked_at IS NOT NULL OR expires_at <= now())"
)


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


# Whether a `loom.namespaces` row's `contact_email` is verified, so another spelling of
# its owner: an SSO account's (no password) or one linked in `loom.sso_links`. Binds
# `{org:String}`.
VERIFIED_EMAIL_SQL = (
    "(password_hash = '' OR (org, namespace, contact_email) IN ("
    "SELECT org, namespace, email FROM loom.sso_links FINAL"
    " WHERE org = {org:String} AND linked = 1))"
)
