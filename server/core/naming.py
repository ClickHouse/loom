"""Shared namespace-name validation, and `named()`: how an identity filter reads
the "-" sentinel the SDKs stamp.

One rule set for every place that accepts a namespace name — the
`LOOM_DEFAULT_NAMESPACE` setting, `namespace.register`, and the SPA's
create dialog — so they cannot drift apart.

The shape: 1–128 chars of lowercase `[a-z0-9-]`, starting with a letter
or digit (a trailing hyphen is legal). Callers with *extra* rules (e.g.
the settings validator reserving `personal-*` for per-subject
auto-provisioning) layer them on top.
"""

from __future__ import annotations

import re
from typing import Any

NAMESPACE_MAX_LEN = 128

# The value of an absent optional identity field (user_id, session_id, run_id)
# on the wire and in every row.
SENTINEL = "-"
# Namespaces single sign-on creates on a first sign-in (`u-<name>-<hash>`).
# Their names follow from the email, so where it creates them nobody else
# may: it would be waiting, with its creator's grants, for that person's
# first sign-in.
SSO_NAMESPACE_PREFIX = "u-"


# The names JIT tries (server/auth/jit.py): `u-`, the email's local part (up
# to 40), 8 hex of its hash, then maybe `-2` to `-6` or 8 more random hex.
_SSO_NAMESPACE_RE = re.compile(
    rf"{SSO_NAMESPACE_PREFIX}.{{1,40}}-[0-9a-f]{{8}}(-[2-6]|-[0-9a-f]{{8}})?"
)


def reserved_for_sso(namespace: str) -> bool:
    """Whether a new namespace by this name is single sign-on's to create."""
    from server.settings import settings

    return settings.oidc_jit_provision and bool(_SSO_NAMESPACE_RE.fullmatch(namespace))


def named(value: str | None, *, wildcard: bool = False) -> str | None:
    """An identity filter's value, or None when it names nothing: empty, or the
    SENTINEL the SDKs stamp on every call that names no user, session or run.
    With `wildcard`, "*" (every agent / session) names nothing too."""
    if not value or value == SENTINEL or (wildcard and value == "*"):
        return None
    return value


def is_anonymous_user(user_id: str) -> bool:
    """Whether `user_id` names no user: the sentinel, or sent empty (stored as given)."""
    return named(user_id) is None


def session_spellings(session_id: str) -> list[str]:
    """The stored session ids that mean `session_id`: no session is '-' or sent empty
    (stored as given), so either spelling is the other."""
    return [SENTINEL, ""] if named(session_id) is None else [session_id]


def same_session(a: str, b: str) -> bool:
    """Whether two stored session ids are one session ('-' and "" are both none)."""
    return b in session_spellings(a)


def present(value: Any) -> str | None:
    """An identity field as the gates read it: None when missing, blank or the
    "-" sentinel, else the value stripped."""
    return named(str(value or "").strip())


def names_session(session_id: str) -> bool:
    """Whether a session id names a session rather than "not given"."""
    return named(session_id.strip()) is not None


_NAMESPACE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def validate_namespace_name(value: str) -> str:
    """Return the stripped name, or raise ``ValueError`` describing why it
    can't be used as a namespace."""
    v = (value or "").strip()
    if not v:
        raise ValueError("namespace name is empty")
    if len(v) > NAMESPACE_MAX_LEN:
        raise ValueError(
            f"namespace name is {len(v)} characters; the maximum is {NAMESPACE_MAX_LEN}"
        )
    if not _NAMESPACE_RE.fullmatch(v):
        raise ValueError(
            f"{v!r} is not a valid namespace name. Use lowercase letters, "
            "digits and hyphens, starting with a letter or digit "
            "(e.g. 'team-alpha')."
        )
    return v


def slugify(text: str, *, max_len: int) -> str:
    """Lowercase ASCII letters and digits, every other run collapsed to one
    hyphen, trimmed of hyphens, cut to `max_len`: "Custom Chat (loom bridge)"
    → "custom-chat-loom-bridge". A bare slug; callers add their prefix and
    their fallback for an empty result."""
    cleaned: list[str] = []
    prev_hyphen = False
    for ch in text.lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            cleaned.append(ch)
            prev_hyphen = False
        elif not prev_hyphen:
            cleaned.append("-")
            prev_hyphen = True
    return "".join(cleaned).strip("-")[:max_len]
