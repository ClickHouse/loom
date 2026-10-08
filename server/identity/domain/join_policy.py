"""How a namespace is joined.

`loom.namespaces.join_policy` is one of:

* ``inherit``   — (default) the rule visibility implies: a
                  viewer self-join on a ``public`` row is active at once unless
                  ``requires_approval``; every other self-join pends;
* ``open``      — a viewer self-join is active at once (writer/admin still pend);
* ``approval``  — every self-join pends until a namespace admin approves;
* ``closed``    — self-join is refused; membership only through grants.create.

Code namespaces (``kind = 'code'``) have two roles: ``viewer``
is a *reader*, ``admin`` is a *maintainer* who approves readers, names other
maintainers and sets the join rule. ``writer`` does not exist on one — the
indexer is the only writer — and is refused everywhere. A person may ask for
either role when joining; a reader join on an ``open`` repository is active at
once, a maintainer request always pends, and any current maintainer decides.
"""

from __future__ import annotations

from typing import Any

INHERIT = "inherit"
OPEN = "open"
APPROVAL = "approval"
CLOSED = "closed"
EXPLICIT_POLICIES = (OPEN, APPROVAL, CLOSED)

CODE_KIND = "code"
CODE_MEMBER_ROLE = "viewer"
CODE_MAINTAINER_ROLE = "admin"
CODE_ROLES = (CODE_MEMBER_ROLE, CODE_MAINTAINER_ROLE)


def effective_join_policy(row: dict[str, Any] | None) -> str:
    """Resolve a directory row to open / approval / closed.

    ``inherit`` (and an empty value) fold ``visibility`` and
    ``requires_approval`` into the word: public and not requires_approval →
    open; otherwise approval."""
    if not row:
        return APPROVAL
    raw = str(row.get("join_policy") or INHERIT).strip().lower()
    if raw in EXPLICIT_POLICIES:
        return raw
    public = str(row.get("visibility") or "private") == "public"
    if public and not bool(row.get("requires_approval", False)):
        return OPEN
    return APPROVAL


def code_role_ceiling_violation(kind: str | None, role: str) -> str | None:
    """The refusal sentence when `role` cannot exist on a namespace of `kind`,
    else None. Only code namespaces have a ceiling, and it refuses exactly one
    word: ``writer``. Readers (viewer) and maintainers (admin) are both real
    roles a person may request or be given; who may confer a
    maintainer is the transport's ADMIN gate, not this function."""
    if (kind or "memory") != CODE_KIND or role in CODE_ROLES:
        return None
    return (
        "repositories have readers and maintainers: nobody writes to an index, "
        f"so '{role}' does not exist on one — ask for 'viewer' (reader) or 'admin' (maintainer)"
    )


def join_gate(*, policy: str, role: str) -> str:
    """Decide a self-join under a RESOLVED policy: 'active', 'pending' or 'closed'.

    open      → viewer active at once; writer/admin pend.
    approval  → everything pends.
    closed    → refused.
    """
    if policy == CLOSED:
        return "closed"
    if role != CODE_MEMBER_ROLE:
        return "pending"
    return "active" if policy == OPEN else "pending"
