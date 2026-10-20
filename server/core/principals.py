"""The grant subjects of collections besides Loom's own accounts
(server/auth/gateway.py): an end user `user:<id>`, a group `group:<id>`, an
agent `agent:<id>`, and `everyone`, which every request acting in the org
holds. An id is lowercase: each principal has exactly one spelling."""

from __future__ import annotations

import hashlib
import re

USER, GROUP, AGENT = "user:", "group:", "agent:"
PRINCIPAL_PREFIXES = (USER, GROUP, AGENT)
EVERYONE = "everyone"
# Subjects with one spelling: service accounts and these principals.
ONE_SPELLING = ("sa:", *PRINCIPAL_PREFIXES)
_ID = re.compile(r"[a-z0-9][a-z0-9._@-]{0,127}")


def is_principal_id(ident: str) -> bool:
    """Whether `ident` is a well-formed principal id (lowercase, as `_ID`)."""
    return _ID.fullmatch(ident) is not None


def principal_id(value: str, prefix: str) -> str:
    """The id of `value` when it is `prefix<id>` with a well-formed id, else ''."""
    if not value.startswith(prefix):
        return ""
    ident = value[len(prefix) :]
    return ident if is_principal_id(ident) else ""


def is_reserved_subject(subject: str) -> bool:
    """True for anything spelled like a grant principal, in any case: only an
    end-user issuer's token may produce one (not a Loom token, not a trusted
    caller), and `everyone` none at all."""
    lowered = subject.lower()
    return lowered.startswith(PRINCIPAL_PREFIXES) or lowered == EVERYONE


def is_principal(subject: str) -> bool:
    """True for `everyone`, and a `user:`, `group:` or `agent:` subject with a
    well-formed id."""
    return subject == EVERYONE or any(principal_id(subject, p) for p in PRINCIPAL_PREFIXES)


# A user's personal collection: named from the org and the user, so its name
# says whose it is (server/auth/sharing.py).
PERSONAL_PREFIX = "col-me-"


def personal_name(org: str, subject: str) -> str:
    """The personal collection of the end user `subject` in `org`."""
    return PERSONAL_PREFIX + hashlib.sha256(f"{org}\0{subject}".encode()).hexdigest()[:24]


def end_user_revocation_subject(org: str, subject: str) -> str:
    """The subject an end user's tokens are revoked by: `<org>/user:<id>`. A
    user id holds no `/`, so the last `/` ends the org: no other org's user
    spells the same key."""
    return f"{org}/{subject}"
