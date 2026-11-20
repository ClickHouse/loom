"""Connection replacement and rotation cannot revive a disconnected account."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from server.knowledge.domain.connected_accounts import (
    ConnectedAccount,
    accepts_callback,
    refreshing,
    rotated,
    source_matches,
    stopped,
)


def _account() -> ConnectedAccount:
    return ConnectedAccount(
        org="team",
        subject="person-1",
        kind="gdrive",
        connection_id="connection-1",
        account_id="provider-person",
        workspace_id="provider-workspace",
        account_email_enc="sealed-email",
        credential_enc="sealed-refresh",
        scopes=("drive.readonly",),
        active=True,
        connected_at=datetime(2026, 1, 1, tzinfo=UTC),
        version=10,
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"org": "other-team"},
        {"owner": "other-person"},
        {"kind": "notion"},
        {"kind": "unsupported"},
        {"connection_id": "replacement-connection"},
        {"connection_id": ""},
    ],
)
def test_source_cannot_borrow_another_registration(changed: dict[str, str]) -> None:
    binding = dict(org="team", owner="person-1", kind="gdrive", connection_id="connection-1")
    assert source_matches(_account(), **binding)
    assert not source_matches(_account(), **(binding | changed))


def test_stop_clears_provider_data_and_fences_pending_refresh_and_callback() -> None:
    account = refreshing(_account(), refresh_id="rotation-1")
    when = datetime(2026, 1, 2, tzinfo=UTC)
    dead = stopped(account, connection_id="stop-1", at=when)
    assert not dead.active
    assert dead.connection_id == "stop-1"
    assert dead.disconnected_at == when
    assert (dead.org, dead.subject, dead.kind) == (account.org, account.subject, account.kind)
    assert (dead.account_id, dead.workspace_id, dead.account_email_enc, dead.credential_enc) == (
        "",
        "",
        "",
        "",
    )
    assert dead.scopes == ()
    assert dead.refresh_id == ""
    assert not accepts_callback(dead, "connection-1")
    assert not source_matches(
        dead, org="team", owner="person-1", kind="gdrive", connection_id="stop-1"
    )
    with pytest.raises(ValueError, match="connection_changed"):
        rotated(
            dead,
            expected_connection="connection-1",
            refresh_id="rotation-1",
            credential_enc="late-sealed-refresh",
        )


def test_disconnect_of_never_connected_account_invalidates_pending_callback() -> None:
    assert accepts_callback(None, "")
    absent = ConnectedAccount(org="team", subject="person-1", kind="notion", connection_id="")
    tombstone = stopped(
        absent, connection_id="stop-before-callback", at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert not accepts_callback(tombstone, "")
    assert accepts_callback(tombstone, "stop-before-callback")


def test_one_refresh_result_cannot_overwrite_replacement_or_another_rotation() -> None:
    pending = refreshing(_account(), refresh_id="rotation-1")
    with pytest.raises(ValueError, match="connection_unavailable"):
        refreshing(pending, refresh_id="rotation-2")
    for current, generation, nonce in (
        (replace(pending, connection_id="connection-2"), "connection-1", "rotation-1"),
        (pending, "connection-1", "rotation-2"),
        (replace(pending, active=False), "connection-1", "rotation-1"),
        (replace(pending, credential_enc=""), "connection-1", "rotation-1"),
    ):
        with pytest.raises(ValueError, match="connection_changed"):
            rotated(
                current,
                expected_connection=generation,
                refresh_id=nonce,
                credential_enc="sealed-replacement",
            )
    changed = rotated(
        pending,
        expected_connection="connection-1",
        refresh_id="rotation-1",
        credential_enc="sealed-replacement",
    )
    assert changed == replace(pending, credential_enc="sealed-replacement", refresh_id="")
    assert source_matches(
        changed, org="team", owner="person-1", kind="gdrive", connection_id="connection-1"
    )
    with pytest.raises(ValueError, match="connection_changed"):
        rotated(
            changed,
            expected_connection="connection-1",
            refresh_id="rotation-1",
            credential_enc="replayed-sealed-replacement",
        )


@pytest.mark.parametrize("generation", ["", "connection-1"])
def test_stop_requires_a_new_generation(generation: str) -> None:
    with pytest.raises(ValueError, match="connection_identity_required"):
        stopped(_account(), connection_id=generation, at=datetime(2026, 1, 2, tzinfo=UTC))


def test_account_representation_contains_no_identity_or_credentials() -> None:
    account = _account()
    displayed = repr(account)
    for secret in (
        account.org,
        account.subject,
        account.account_id,
        account.workspace_id,
        account.account_email_enc,
        account.credential_enc,
    ):
        assert secret not in displayed
