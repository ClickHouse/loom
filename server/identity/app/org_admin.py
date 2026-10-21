"""What an org admin's op checks about its target and its caller, for the ops
that set an org-wide control (`org.set_model_calls`, `extraction_policy.set`)."""

from __future__ import annotations

from server.core.errors import ValidationError


def one_org(org: str) -> None:
    """Refuse a value that names no org: "", "-" (the identity tuple's none),
    "*" (a wildcard) or one with surrounding space."""
    if org.strip() in ("", "-", "*") or org != org.strip():
        raise ValidationError(f"org must name one org, got {org!r}")


def caller() -> tuple[str, bool]:
    """(the caller's subject, whether it is a platform admin). With auth off
    (development) there is no caller: "admin", and nothing to lock against."""
    from server.auth.context import current_auth
    from server.operations.base import Role

    auth = current_auth()
    if auth is None:
        return "admin", True
    return auth.subject or "admin", auth.has_role(Role.PLATFORM_ADMIN)
