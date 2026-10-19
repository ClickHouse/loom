"""The grant subjects a gateway's end users are known by (server/auth/gateway.py):
`user:<id>`, `group:<id>` and `agent:<id>`. An id is lowercase: each principal
has exactly one spelling."""

from __future__ import annotations

import hashlib
import re

USER, GROUP, AGENT = "user:", "group:", "agent:"
PRINCIPAL_PREFIXES = (USER, GROUP, AGENT)
# Subjects with one spelling: service accounts and these principals.
ONE_SPELLING = ("sa:", *PRINCIPAL_PREFIXES)
_ID = re.compile(r"[a-z0-9][a-z0-9._@-]{0,127}")


def principal_id(value: str, prefix: str) -> str:
    """The id of `value` when it is `prefix<id>` with a well-formed id, else ''."""
    if not value.startswith(prefix):
        return ""
    ident = value[len(prefix) :]
    return ident if _ID.fullmatch(ident) else ""


def is_reserved_subject(subject: str) -> bool:
    """True for anything spelled like a grant principal, in any case: only a
    gateway's assertion may produce one (not a token, not a trusted caller)."""
    return subject.lower().startswith(PRINCIPAL_PREFIXES)


def is_principal(subject: str) -> bool:
    """True for a `user:`, `group:` or `agent:` subject with a well-formed id."""
    return any(principal_id(subject, p) for p in PRINCIPAL_PREFIXES)


# A user's personal collection: named from the org and the user, so its name
# says whose it is (server/auth/sharing.py).
PERSONAL_PREFIX = "col-me-"


def personal_name(org: str, subject: str) -> str:
    """The personal collection of the end user `subject` in `org`."""
    return PERSONAL_PREFIX + hashlib.sha256(f"{org}\0{subject}".encode()).hexdigest()[:24]
