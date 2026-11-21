"""Root rotation rewrites all historical approval envelopes without replacing authority."""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest

from server.auth import source_registration as registration
from server.auth.jwt import AuthorizationClaims
from server.core.secrets_at_rest import seal, unseal
from server.knowledge.app import crawl_reseal
from server.operations.base import Role
from server.settings import settings
from tests.unit.test_crawl_restore import _FIRST, _SECOND, _HistoryWorld

pytestmark = pytest.mark.asyncio


def _approval(source_id: str, generation: str, *, org: str = "org") -> tuple[str, str]:
    scope = registration.Scope(org, "namespace", source_id, generation, "f" * 64)
    claims = AuthorizationClaims(
        subject="registrant",
        org=org,
        namespaces=("namespace",),
        roles=(Role.ORG_ADMIN,),
        is_service_account=False,
        issued_at=1,
        expires_at=2,
        creator="original-creator",
    )
    plain = json.dumps(
        {
            "format": 1,
            "scope": dataclasses.asdict(scope),
            "registrant": "registrant",
            "claims": dataclasses.asdict(claims),
        },
        sort_keys=True,
    )
    return seal(registration.PURPOSE, plain), plain


async def _history(monkeypatch):
    world = _HistoryWorld(monkeypatch)
    monkeypatch.setattr(crawl_reseal, "get_client", lambda: world)
    monkeypatch.setattr(settings, "knowledge_crawl_registry_page_size", 1)
    first, _ = _approval(_FIRST, "earlier-generation")
    second, _ = _approval(_FIRST, "later-generation")
    third, _ = _approval(_SECOND, "other-source-generation")
    await world.seed(approval_enc=first)
    await world.seed(approval_enc=first)
    await world.seed(approval_enc=second)
    await world.seed(name="secondary", source_id=_SECOND, approval_enc=third)
    await world.seed(name="shared", credential_mode="shared", approval_enc="unrelated")
    return world


def _rotate(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret_previous", settings.jwt_secret)
    monkeypatch.setattr(
        settings, "jwt_secret", "replacement-root-sealing-secret-which-is-long-enough"
    )


async def test_approval_reseal_reaches_every_raw_version_and_retains_exact_source_identity(
    monkeypatch,
):
    world = await _history(monkeypatch)
    before = copy.deepcopy(world.sources)
    plaintext = [unseal(registration.PURPOSE, r["approval_enc"]) for r in before[:4]]
    _rotate(monkeypatch)
    assert await crawl_reseal.approvals(dry_run=False, ch=world) == (3, [])
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    for old, row, plain in zip(before[:4], world.sources[:4], plaintext, strict=True):
        assert row["approval_enc"] != old["approval_enc"]
        assert unseal(registration.PURPOSE, row["approval_enc"], warn=False) == plain
        assert not unseal(registration.PURPOSE, old["approval_enc"], warn=False)
        frame = json.loads(plain)
        scope = registration.Scope(**frame["scope"])
        approval = registration.load(row["approval_enc"], scope, registrant="registrant")
        assert approval.scope.source_id == row["source_id"]
        assert approval.scope == scope and approval.registrant == frame["registrant"]
        assert approval.claims.roles == (Role.ORG_ADMIN,)
        assert (
            approval.claims.subject == "registrant"
            and approval.claims.creator == "original-creator"
        )
        assert approval.claims.expires_at == 2
        assert {k: v for k, v in row.items() if k != "approval_enc"} == {
            k: v for k, v in old.items() if k != "approval_enc"
        }
    assert world.sources[-1] == before[-1]
    assert len(world.page_reads) >= 3 and not world.retirements


async def test_approval_reseal_dry_run_counts_old_envelopes_without_writing_history(monkeypatch):
    world = await _history(monkeypatch)
    before = copy.deepcopy(world.sources)
    _rotate(monkeypatch)
    assert await crawl_reseal.approvals(dry_run=True, ch=world) == (3, [])
    assert world.sources == before and not world.executed and not world.retirements


async def test_unreadable_approval_is_reported_without_resetting_its_source(monkeypatch):
    world = _HistoryWorld(monkeypatch)
    monkeypatch.setattr(crawl_reseal, "get_client", lambda: world)
    await world.seed(approval_enc="unreadable-ciphertext")
    before = copy.deepcopy(world.sources)
    _rotate(monkeypatch)
    assert await crawl_reseal.approvals(dry_run=False, ch=world) == (0, ["org/namespace/documents"])
    assert world.sources == before and not world.executed


async def test_approval_copied_from_another_scope_cannot_be_resealed_as_local_authority(
    monkeypatch,
):
    world = _HistoryWorld(monkeypatch)
    monkeypatch.setattr(crawl_reseal, "get_client", lambda: world)
    foreign, _ = _approval(_FIRST, "generation", org="another-org")
    await world.seed(approval_enc=foreign)
    before = copy.deepcopy(world.sources)
    _rotate(monkeypatch)
    with pytest.raises(registration.Refused):
        await crawl_reseal.approvals(dry_run=False, ch=world)
    assert world.sources == before and not world.executed


async def test_ignored_history_reseal_fails_its_raw_ciphertext_residue_check(monkeypatch):
    world = await _history(monkeypatch)
    before = copy.deepcopy(world.sources)
    world.ignore_mutation = True
    _rotate(monkeypatch)
    with pytest.raises(RuntimeError, match="crawl_approval_reseal_residue"):
        await crawl_reseal.approvals(dry_run=False, ch=world)
    assert world.executed and world.sources == before


async def test_approval_reseal_refuses_another_target_without_touching_history(monkeypatch):
    world = await _history(monkeypatch)
    before = copy.deepcopy((world.sources, world.page_reads, world.executed))
    with pytest.raises(RuntimeError, match="crawl_reseal_target_mismatch"):
        await crawl_reseal.approvals(dry_run=False, ch=object())
    assert (world.sources, world.page_reads, world.executed) == before
