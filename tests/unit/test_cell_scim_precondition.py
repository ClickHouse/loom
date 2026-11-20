"""Attach checks actual provisioning while the first cell is still recording."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auth import authz, revocation, scim, subject_revocation
from server.cli import cell_attach, cell_attach_beats
from server.cli.cell_ch import Refused
from server.core import cell, ch
from server.settings import settings
from tests.unit.test_cell_attach import FIELDS, FakeConn, _args, _beat, _value
from tests.unit.test_scim_people import People


class Census(People):
    def __init__(self):
        super().__init__()
        self.sessions = []
        self.services_census = []
        self.revocations = {}
        self.writes = []
        self.fail_revocation = None
        self.session_states = {}
        self.census_reads = []

    async def query_one(self, sql, params=None):
        p = params or {}
        if "AS identity_bytes" in sql:
            self.census_reads.append(sql)
            if self.unavailable:
                raise ConnectionError("identity store unavailable")
            if "loom.namespaces" in sql:
                rows = [
                    (org, f"account-{i:05}", org, owner, "", "", 0, 1)
                    for i, (org, owner, _email, _verified) in enumerate(self.accounts)
                ]
            elif "loom.oauth_state" in sql:
                rows = self.sessions
            elif "loom.service_accounts" in sql:
                rows = self.services_census
            else:
                raise AssertionError(sql)
            selected = sorted(
                row for row in rows if not p["started"] or row[:2] > (p["after0"], p["after1"])
            )[:128]
            size = sum(sum(len(value.encode()) for value in row[:6]) for row in selected)
            return {
                "n": len(selected),
                "bytes": size,
                "identities": selected if size <= p["bytes"] else [],
            }
        if "loom.oauth_signin_state" in sql:
            value = self.session_states.get(p["k"])
            return {"value": value} if value is not None else None
        return await super().query_one(sql, params)

    async def query_all(self, sql, params=None):
        if "loom.revoked_tokens" in sql:
            return [self.revocations[key] for key in params["keys"] if key in self.revocations]
        return await super().query_all(sql, params)


@pytest.fixture
def provisioned_run(monkeypatch):
    world = Census()
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "scim_token", "lsc_first_" + "a" * 64)
    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    monkeypatch.setattr(settings, "document_s3_prefix", "")
    monkeypatch.setattr(cell, "mode", lambda: "recording")
    monkeypatch.setattr(ch, "get_client", lambda: world)
    monkeypatch.setattr(scim, "get_client", lambda: world)
    monkeypatch.setattr(authz, "_ENDED", {})
    native = {"mode": "recording", "first": True}
    run = cell_attach.Run(world, _args(), SimpleNamespace(), lambda _text: None)
    run.row = {
        "cell": "first",
        "is_first": True,
        "deployment_id": "dep-1",
        "bucket": "",
        "bucket_prefix": "",
    }
    run.own_fp = (cell.as_value({**FIELDS, "scim": True}), None)
    now = datetime.now(UTC) - timedelta(minutes=1)

    def state():
        return {
            "deployment_id": "dep-1",
            "is_first": native["first"],
            "cell": "first",
            "mode": native["mode"],
            "since": now,
            "updated_at": now,
            "current_fp": run.own_fp[0],
            "next_fp": run.own_fp[1],
        }

    run.lock_conn = FakeConn(
        [
            ("SELECT deployment_id FROM loom.authority", "dep-1"),
            ("SELECT a.deployment_id, f.is_first", lambda: state()),
        ]
    )

    async def read_beats(_client):
        return [_beat(value={**_value(), **run.own_fp[0]})]

    @asynccontextmanager
    async def subject_leases(_subjects):
        yield

    async def record(subject, *, issued_before, **_kwargs):
        if world.fail_revocation == subject:
            raise ConnectionError("revocation store unavailable")
        written = {
            "jti": revocation.SCIM_PREFIX + subject,
            "issued_before": issued_before,
            "revoked_by": "cli:operator",
            "revoked_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(days=365),
            "reason": "scim: unprovisioned at cell attach",
        }
        world.revocations[written["jti"]] = written
        world.writes.append(written)
        return written

    monkeypatch.setattr(cell_attach_beats, "read_beats", read_beats)
    monkeypatch.setattr(scim, "subject_leases", subject_leases)
    monkeypatch.setattr(revocation, "scim_revoke", record)
    monkeypatch.setattr(subject_revocation, "audit_revocation", AsyncMock())
    return run, world, native


async def _accepted(run):
    try:
        observed = await run.check_beats("refuse")
    except Refused as refused:
        observed = refused
    assert not isinstance(observed, Refused), str(observed)
    return observed


async def test_recording_active_person_passes_actual_run(provisioned_run):
    run, people, _native = provisioned_run
    people.accounts = [("o", "person@example.test", "person@example.test", True)]
    people.provision("person@example.test")
    verdict = await _accepted(run)
    assert verdict.scim is True
    assert len(people.census_reads) == 3
    assert people.writes == []


async def test_scim_off_control_preserves_actual_run(provisioned_run, monkeypatch):
    run, people, _native = provisioned_run
    monkeypatch.setattr(settings, "scim_token", "")
    run.own_fp = (cell.as_value({**FIELDS, "scim": False}), None)
    people.unavailable = True
    assert (await run.check_beats("refuse")).scim is False
    assert people.census_reads == []
    assert people.writes == []


async def test_recording_missing_contact_names_actual_owner(provisioned_run):
    run, people, _native = provisioned_run
    people.accounts = [
        ("o", "person@example.test", "person@example.test", True),
        ("o", "person@example.test", "password@example.test", False),
    ]
    people.provision("person@example.test")
    with pytest.raises(Refused) as refused:
        await run.check_beats("refuse")
    assert "unprovisioned accounts subject 'person@example.test'" in str(refused.value)
    assert people.writes == []


async def test_actual_live_refresh_uses_its_stored_verified_email(provisioned_run):
    run, people, _native = provisioned_run
    people.sessions = [("", "refresh-id", "o", "handle", "person@example.test", "family", 0, 1)]
    people.session_states["family:family"] = "0:refresh-id"
    people.provision("person@example.test")
    assert (await _accepted(run)).scim is True
    assert run.report["scim"]["checked"] == 1
    assert people.writes == []


async def test_personal_sa_checks_creator_even_without_inheritance(provisioned_run):
    run, people, _native = provisioned_run
    people.services_census = [("o", "personal", "o", "sa:personal", "", "", 0, 1)]
    people.services["sa:personal"] = {"created_by": "person@example.test", "lineage": []}
    people.provision("person@example.test")
    assert (await _accepted(run)).scim is True
    assert people.writes == []


async def test_actual_registered_bot_remains_exempt(provisioned_run):
    run, people, _native = provisioned_run
    people.services_census = [("o", "automation", "o", "sa:automation", "", "", 0, 1)]
    people.bots["automation"] = True
    assert (await _accepted(run)).scim is True
    assert people.writes == []


@pytest.mark.parametrize(
    "tokens,reason",
    [
        ("lsc_other_" + "b" * 64, "names another cell"),
        (",".join(["lsc_first_" + "a" * 64] * 3), "at most two"),
    ],
)
async def test_actual_run_applies_cell_token_policy(provisioned_run, monkeypatch, tokens, reason):
    run, people, _native = provisioned_run
    monkeypatch.setattr(settings, "scim_token", tokens)
    with pytest.raises(Refused) as refused:
        await run.check_beats("refuse")
    assert reason in str(refused.value)
    assert people.census_reads == []
    assert people.writes == []


async def test_revoke_unprovisioned_contracts_without_provisioning(provisioned_run):
    run, people, _native = provisioned_run
    run.args.revoke_unprovisioned = True
    people.accounts = [("o", "person@example.test", "person@example.test", True)]
    assert (await _accepted(run)).scim is True
    assert len(people.writes) == 1
    assert people.writes[0]["jti"] == revocation.SCIM_PREFIX + "person@example.test"
    assert people.writes[0]["issued_before"] is not None
    assert people.users == {}
    assert run.report["scim"]["revoked"] == 1
    assert run.report["scim"]["complete"] is True


async def test_partial_contraction_retains_prior_accepted_cutoff(provisioned_run):
    run, people, _native = provisioned_run
    run.args.revoke_unprovisioned = True
    people.accounts = [
        ("o", "first@example.test", "first@example.test", True),
        ("o", "second@example.test", "second@example.test", True),
    ]
    people.fail_revocation = "second@example.test"
    with pytest.raises(Refused):
        await run.check_beats("refuse")
    assert len(people.writes) == 1
    assert people.writes[0]["jti"] == revocation.SCIM_PREFIX + "first@example.test"
    assert run.report["scim"]["revoked"] == 1
    assert run.report["scim"]["complete"] is False
    assert people.users == {}


async def test_dry_run_with_revoke_flag_does_not_contract(provisioned_run):
    run, people, _native = provisioned_run
    run.args.revoke_unprovisioned = True
    run.args.dry_run = True
    people.accounts = [("o", "person@example.test", "person@example.test", True)]
    with pytest.raises(Refused) as refused:
        await run.check_beats("refuse")
    assert "unprovisioned accounts" in str(refused.value)
    assert people.writes == []
    assert people.users == {}


@pytest.mark.parametrize("outright", [True, False], ids=["outright", "newer-cutoff"])
async def test_contraction_never_weakens_existing_scim_row(provisioned_run, outright):
    run, people, native = provisioned_run
    run.args.revoke_unprovisioned = True
    native["mode"] = "enforced"
    people.accounts = [("o", "person@example.test", "person@example.test", True)]
    key = revocation.SCIM_PREFIX + "person@example.test"
    existing = {
        "jti": key,
        "issued_before": None if outright else datetime.now(UTC) + timedelta(seconds=5),
        "revoked_by": "scim",
    }
    people.revocations[key] = existing.copy()
    assert (await _accepted(run)).scim is True
    assert people.revocations[key] == existing
    assert people.writes == []
    assert run.report["scim"]["revoked"] == 0


async def test_missing_provisioning_does_not_borrow_authentication_memo(provisioned_run):
    run, people, _native = provisioned_run
    people.accounts = [("o", "person@example.test", "person@example.test", True)]
    # Populate the real ordinary helper while recording: deactivation-only allows it.
    assert await scim.refuses_subject("person@example.test", org="o") is False
    with pytest.raises(Refused) as refused:
        await run.check_beats("refuse")
    assert "unprovisioned accounts subject" in str(refused.value)
    assert people.writes == []
