"""`grants.create` never lowers a grant by accident.

`loom.namespace_grants` is a ReplacingMergeTree keyed on (org, namespace,
subject): one insert IS the subject's whole grant, so a `viewer` create for a
current `admin` would demote them silently. The handler reads the current
active grant first: same role or higher
goes through; lower is 409 GRANT_WOULD_DOWNGRADE unless `replace=true`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.core.errors import GrantWouldDowngradeError
from server.identity.api.models_grants import GrantsCreateInput
from server.identity.app import grants as grants_mod
from server.identity.domain.grants import GrantState
from server.identity.store import grants as grants_repo

# A revoke reads no directory ties here (`conftest.no_directory_ties`).
pytestmark = pytest.mark.usefixtures("no_directory_ties", "off_code_repos")


def _wire(
    monkeypatch: pytest.MonkeyPatch, existing: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    inserted: list[dict[str, Any]] = []
    looked_up: list[tuple[str, str, str]] = []

    async def _find(org: str, ns: str, sub: str, **_: Any) -> GrantState | None:
        looked_up.append((org, ns, sub))
        return None if existing is None else GrantState.from_row(existing)

    async def _insert(row: dict[str, Any], **_: Any) -> None:
        inserted.append(row)

    async def _no_ceiling(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(grants_repo, "find_active_grant", _find)
    monkeypatch.setattr(grants_repo, "find_any_grant", _find)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(grants_mod, "_enforce_code_ceiling", _no_ceiling)
    return inserted, looked_up


def _row(role: str, **extra: Any) -> dict[str, Any]:
    return {
        "role": role,
        "granted_by": "ops",
        "status": "active",
        "pending_role": None,
        "pending_since": None,
        **extra,
    }


def _create(role: str, *, replace: bool = False) -> GrantsCreateInput:
    return GrantsCreateInput(
        org="o",
        namespace="ns",
        subject="alice",
        role=role,
        replace=replace,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_first_grant_inserts_and_reads_the_current_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted, looked_up = _wire(monkeypatch, None)
    out = await grants_mod.handle_grants_create(_create("viewer"))
    assert looked_up == [("o", "ns", "alice")]
    assert out.role == "viewer"
    assert len(inserted) == 1 and inserted[0]["role"] == "viewer"


@pytest.mark.asyncio
async def test_same_role_recreate_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    inserted, _ = _wire(monkeypatch, _row("writer"))
    out = await grants_mod.handle_grants_create(_create("writer"))
    assert out.role == "writer"
    assert [r["role"] for r in inserted] == ["writer"]
    assert inserted[0]["status"] == "active" and inserted[0]["revoked_at"] is None


@pytest.mark.asyncio
async def test_upgrade_is_allowed_without_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    inserted, _ = _wire(monkeypatch, _row("viewer"))
    out = await grants_mod.handle_grants_create(_create("admin"))
    assert out.role == "admin"
    assert inserted[0]["role"] == "admin"


@pytest.mark.asyncio
async def test_downgrade_without_replace_is_409_with_typed_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted, _ = _wire(monkeypatch, _row("admin"))
    with pytest.raises(GrantWouldDowngradeError) as ei:
        await grants_mod.handle_grants_create(_create("viewer"))
    err = ei.value
    assert err.status_code == 409
    detail = err.to_detail()
    assert detail["code"] == "GRANT_WOULD_DOWNGRADE"
    assert detail["current_role"] == "admin" and detail["requested_role"] == "viewer"
    # The message names the current role and how to do it on purpose.
    assert "admin" in detail["message"] and "replace=true" in detail["message"]
    assert "grants.revoke" in detail["message"]
    assert inserted == [], "a refused create must not write"


def _admins(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    async def _count(*_a: Any, **_k: Any) -> int:
        return n

    monkeypatch.setattr(grants_repo, "count_active_admins", _count)


@pytest.mark.asyncio
async def test_downgrade_with_replace_overwrites(monkeypatch: pytest.MonkeyPatch) -> None:
    inserted, _ = _wire(monkeypatch, _row("admin"))
    _admins(monkeypatch, 2)
    out = await grants_mod.handle_grants_create(_create("viewer", replace=True))
    assert out.role == "viewer"
    assert inserted[0]["role"] == "viewer"


@pytest.mark.asyncio
async def test_writer_to_viewer_is_also_a_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _row("writer"))
    with pytest.raises(GrantWouldDowngradeError):
        await grants_mod.handle_grants_create(_create("viewer"))


@pytest.mark.asyncio
async def test_insert_mirrors_every_grant_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """RMT rule: the new version row carries every persisted column."""
    inserted, _ = _wire(monkeypatch, None)
    await grants_mod.handle_grants_create(_create("viewer"))
    assert set(inserted[0]) == {
        "org",
        "namespace",
        "subject",
        "role",
        "status",
        "pending_role",
        "pending_since",
        "granted_at",
        "granted_by",
        "revoked_at",
        "version",
    }


@pytest.mark.asyncio
async def test_pending_request_the_new_role_does_not_satisfy_is_carried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    since = datetime(2026, 9, 16, 8, 0, tzinfo=UTC).replace(tzinfo=None)
    inserted, _ = _wire(monkeypatch, _row("viewer", pending_role="admin", pending_since=since))
    await grants_mod.handle_grants_create(_create("writer"))
    assert inserted[0]["pending_role"] == "admin"
    assert inserted[0]["pending_since"] == since


@pytest.mark.asyncio
async def test_pending_request_the_new_role_satisfies_is_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    since = datetime(2026, 9, 16, 8, 0, tzinfo=UTC).replace(tzinfo=None)
    inserted, _ = _wire(monkeypatch, _row("viewer", pending_role="writer", pending_since=since))
    await grants_mod.handle_grants_create(_create("writer"))
    assert inserted[0]["pending_role"] is None
    assert inserted[0]["pending_since"] is None


def test_replace_defaults_off_on_the_wire() -> None:
    assert GrantsCreateInput(org="o", namespace="n", subject="s", role="viewer").replace is False


@pytest.mark.asyncio
async def test_an_email_spelling_is_checked_against_the_row_it_would_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is written under the login handle, so the downgrade guard reads that row, not
    the email spelling the granter typed."""
    _inserted, looked_up = _wire(monkeypatch, _row("admin"))

    async def _canonical(org: str, subject: str, **_: Any) -> str:
        return "alice" if subject == "alice@x.com" else subject

    monkeypatch.setattr("server.auth.aliases.canonical_subject", _canonical)
    inp = GrantsCreateInput(org="o", namespace="ns", subject="alice@x.com", role="viewer")
    with pytest.raises(GrantWouldDowngradeError):
        await grants_mod.handle_grants_create(inp)
    assert looked_up == [("o", "ns", "alice")]


@pytest.mark.asyncio
async def test_revoke_by_email_revokes_the_canonical_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import token_grants

    inserted: list[dict[str, Any]] = []

    async def _verified(org: str, subject: str, ch: Any) -> list[str]:
        return [subject, "alice"]

    async def _find_any(org: str, ns: str, sub: str, **_: Any) -> GrantState | None:
        return GrantState.from_row(_row("writer")) if sub == "alice" else None

    async def _insert(row: dict[str, Any], **_: Any) -> None:
        inserted.append(row)

    async def _cascade(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("server.auth.aliases.read_aliases", _verified)
    monkeypatch.setattr(grants_repo, "find_any_grant", _find_any)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(token_grants, "cascade_revoke_to_created_tokens", _cascade)
    out = await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org="o", namespace="ns", subject="alice@x.com")
    )
    assert out.revoked is True
    assert inserted[0]["subject"] == "alice" and inserted[0]["revoked_at"] is not None


@pytest.mark.asyncio
async def test_a_row_taken_under_another_spelling_is_audited_when_its_reply_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The row under mallory's handle may have landed; a retry would find nothing,
    # so its audit row is written whether the insert answered or not, marked so.
    from server.identity.api.models_grants import GrantsRevokeInput

    audited: list[Any] = []

    async def _spellings(org: str, subject: str, **_: Any) -> list[str]:
        return [subject, "mallory"]

    async def _find_any(org: str, ns: str, sub: str, **_: Any) -> GrantState | None:
        return GrantState.from_row(_row("writer")) if sub == "mallory" else None

    async def _lost(row: dict[str, Any], **_: Any) -> None:
        raise TimeoutError("reply lost")

    async def _emit(event_type: str, **kw: Any) -> str:
        audited.append((event_type, kw["subject"]))
        return ""

    monkeypatch.setattr("server.auth.aliases.spellings", _spellings)
    monkeypatch.setattr(grants_repo, "find_any_grant", _find_any)
    monkeypatch.setattr(grants_repo, "insert_row", _lost)
    monkeypatch.setattr("server.observability.audit.emit", _emit)
    from server.core.errors import BackendUnavailableError

    with pytest.raises(BackendUnavailableError):  # the grant's claim reports the timeout
        await grants_mod.handle_grants_revoke(
            GrantsRevokeInput(org="o", namespace="ns", subject="bob@corp.example")
        )
    # Marked unconfirmed: the insert raised, so the row may or may not have landed.
    assert audited == [
        (
            "grants.revoke",
            {
                "namespace": "ns",
                "subject": "mallory",
                "named": "bob@corp.example",
                "landed": "unknown",
            },
        )
    ]


@pytest.mark.asyncio
async def test_revoke_by_email_reaches_a_row_under_an_unverified_accounts_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The address is no verified spelling of `mallory`, and holds no row of its
    # own; the live row of the unverified account naming it is what it revokes.
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import token_grants

    inserted: list[dict[str, Any]] = []

    async def _spellings(org: str, subject: str, **_: Any) -> list[str]:
        return [subject, "mallory"]

    async def _find_any(org: str, ns: str, sub: str, **_: Any) -> GrantState | None:
        return GrantState.from_row(_row("writer")) if sub == "mallory" else None

    async def _insert(row: dict[str, Any], **_: Any) -> None:
        inserted.append(row)

    async def _cascade(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("server.auth.aliases.spellings", _spellings)
    monkeypatch.setattr(grants_repo, "find_any_grant", _find_any)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(token_grants, "cascade_revoke_to_created_tokens", _cascade)
    out = await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org="o", namespace="ns", subject="bob@corp.example")
    )
    assert out.revoked is True
    assert inserted[0]["subject"] == "mallory" and inserted[0]["revoked_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handle", "verified", "email", "revoked"),
    [
        # `mallory`'s unverified email may name someone else's grant: revoking her
        # handle, which holds none here, revokes nothing.
        ("mallory", [], "bob@corp.example", False),
        # An SSO account's verified email is its owner's: the row under it goes.
        ("u-carol", ["carol@corp.example"], "carol@corp.example", True),
    ],
)
async def test_revoke_by_handle_takes_its_verified_email_row_only(
    monkeypatch: pytest.MonkeyPatch, handle: str, verified: list[str], email: str, revoked: bool
) -> None:
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import token_grants

    inserted: list[dict[str, Any]] = []

    async def _read_aliases(org: str, subject: str, ch: Any) -> list[str]:
        return [subject, *verified]

    async def _spellings(org: str, subject: str, **_: Any) -> list[str]:
        return [subject, email]  # every pair, the unverified one too

    async def _find_any(org: str, ns: str, sub: str, **_: Any) -> GrantState | None:
        return GrantState.from_row(_row("writer")) if sub == email else None

    async def _insert(row: dict[str, Any], **_: Any) -> None:
        inserted.append(row)

    async def _cascade(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("server.auth.aliases.read_aliases", _read_aliases)
    monkeypatch.setattr("server.auth.aliases.spellings", _spellings)
    monkeypatch.setattr(grants_repo, "find_any_grant", _find_any)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(token_grants, "cascade_revoke_to_created_tokens", _cascade)
    out = await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org="o", namespace="ns", subject=handle)
    )
    assert out.revoked is revoked
    assert [r["subject"] for r in inserted] == ([email] if revoked else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_as", ["dave@x.io", "dave"])
async def test_one_revoke_takes_the_persons_row_under_each_verified_spelling(
    monkeypatch: pytest.MonkeyPatch, revoked_as: str
) -> None:
    # Granted under the address before it was verified, and under the handle after:
    # the gate admits by both rows, so one revoke takes both.
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import token_grants

    inserted: list[dict[str, Any]] = []

    async def _read_aliases(org: str, subject: str, ch: Any) -> list[str]:
        return [subject, "dave" if "@" in subject else "dave@x.io"]

    async def _find_any(org: str, ns: str, sub: str, **_: Any) -> GrantState | None:
        if sub in {"dave", "dave@x.io"} and sub not in {r["subject"] for r in inserted}:
            return GrantState.from_row(_row("writer"))
        return None

    async def _insert(row: dict[str, Any], **_: Any) -> None:
        inserted.append(row)

    cascaded: list[str] = []

    async def _cascade(org: str, namespace: str, subject: str, **_k: Any) -> None:
        cascaded.append(subject)

    monkeypatch.setattr("server.auth.aliases.read_aliases", _read_aliases)
    monkeypatch.setattr(grants_repo, "find_any_grant", _find_any)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(token_grants, "cascade_revoke_to_created_tokens", _cascade)
    out = await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org="o", namespace="ns", subject=revoked_as)
    )
    assert out.revoked is True
    assert sorted(r["subject"] for r in inserted) == ["dave", "dave@x.io"]
    # One cascade under the subject asked for, whose spellings reach both rows.
    assert cascaded == [revoked_as]


@pytest.mark.asyncio
@pytest.mark.parametrize(("email_role", "admins"), [("admin", 2), ("writer", 1)])
async def test_the_only_admin_under_two_spellings_is_refused_before_any_write(
    monkeypatch: pytest.MonkeyPatch, email_role: str, admins: int
) -> None:
    # The namespace's admin rows are all the person's: nothing is revoked, not
    # even the row that is not the last admin's.
    from server.core.errors import PreconditionFailedError
    from server.identity.api.models_grants import GrantsRevokeInput

    inserted: list[dict[str, Any]] = []
    roles = {"dave": "admin", "dave@x.io": email_role}

    async def _read_aliases(org: str, subject: str, ch: Any) -> list[str]:
        return [subject, "dave@x.io" if subject == "dave" else "dave"]

    async def _find_any(org: str, ns: str, sub: str, **_: Any) -> GrantState | None:
        return GrantState.from_row(_row(roles[sub])) if sub in roles else None

    async def _insert(row: dict[str, Any], **_: Any) -> None:
        inserted.append(row)

    monkeypatch.setattr("server.auth.aliases.read_aliases", _read_aliases)
    monkeypatch.setattr(grants_repo, "find_any_grant", _find_any)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    _admins(monkeypatch, admins)
    with pytest.raises(PreconditionFailedError, match="only admin"):
        await grants_mod.handle_grants_revoke(
            GrantsRevokeInput(org="o", namespace="ns", subject="dave")
        )
    assert inserted == []


@pytest.mark.asyncio
async def test_the_last_admin_is_not_lowered(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import PreconditionFailedError

    inserted, _ = _wire(monkeypatch, _row("admin"))
    _admins(monkeypatch, 1)
    with pytest.raises(PreconditionFailedError, match="only admin"):
        await grants_mod.handle_grants_create(_create("viewer", replace=True))
    assert inserted == []
