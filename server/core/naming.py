"""Shared namespace-name validation.

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

NAMESPACE_MAX_LEN = 128

# The value of an absent optional identity field (user_id, session_id, run_id)
# on the wire and in every row.
SENTINEL = "-"
# Namespaces single sign-on creates on a first sign-in (`u-<name>-<hash>`).
# Their names follow from the email, so nobody else may create one: it would
# be waiting, with its creator's grants, for that person's first sign-in.
SSO_NAMESPACE_PREFIX = "u-"

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
