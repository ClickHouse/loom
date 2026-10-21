"""Unit tests for the profile-consolidation worker — mocked CH/LLM/profile."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from server.memory.workers.profile_consolidation import (
    ProfileConsolidationConfig,
    run_pass,
)


class StubCH:
    def __init__(self, identities, memories, last=None) -> None:
        self._identities = identities
        self._memories = [
            {"memory_id": str(UUID(int=i + 1)), **row} for i, row in enumerate(memories)
        ]
        self._last = last

    async def query_all(self, sql, params=None):
        # The identity scan groups by org; the per-user fetch doesn't.
        return self._identities if "GROUP BY m.org" in sql else self._memories

    async def query_one(self, sql, params=None):
        return {"last": self._last}


class _LLMOut:
    text = "Prefers vim; deploys Tuesdays and Thursdays."


class StubLLM:
    async def complete(self, system, user, max_tokens):
        return _LLMOut()


def _patch(monkeypatch, ch, *, llm=True):
    import server.core.ch as chmod

    monkeypatch.setattr(chmod, "get_client", lambda: ch)
    if llm:
        import server.llm.client as llmmod

        monkeypatch.setattr(llmmod, "get_llm_client", lambda: StubLLM())


@pytest.mark.asyncio
async def test_folds_stale_identity_into_a_profile_section(monkeypatch) -> None:
    ch = StubCH(
        identities=[{"org": "dev", "namespace": "ns", "user_id": "u1", "n": 6}],
        memories=[{"value": "vim"}, {"value": "deploy Tue"}],
        last=None,  # never consolidated → not on cooldown
    )
    _patch(monkeypatch, ch)
    writes = []
    import server.memory.app.profile as profmod

    async def fake_set(**kwargs):
        writes.append(SimpleNamespace(**kwargs))

    monkeypatch.setattr(profmod, "write_folded_section", fake_set)

    res = await run_pass()
    assert res.identities_scanned == 1
    assert res.profiles_written == 1
    assert writes[0].label == ProfileConsolidationConfig().label
    assert writes[0].user_id == "u1"
    assert writes[0].value  # the folded text


@pytest.mark.asyncio
async def test_the_cooldown_filters_the_scan_before_its_limit(monkeypatch) -> None:
    """A user folded within the cooldown is not a candidate at all. Filtered
    after the LIMIT, the same top identities would come back every pass and be
    skipped, and every user ranked below them would never be folded; filtered
    on `created_at`, which a rewrite keeps, the cooldown would end a day after
    the first fold and never apply again."""
    calls: list[tuple[str, dict]] = []

    class _RecCH(StubCH):
        async def query_all(self, sql, params=None):
            calls.append((sql, dict(params or {})))
            return await super().query_all(sql, params)

        async def query_one(self, sql, params=None):
            raise AssertionError("no per-identity cooldown read")

    ch = _RecCH(identities=[], memories=[])
    _patch(monkeypatch, ch, llm=False)
    res = await run_pass(ProfileConsolidationConfig(cooldown_seconds=3600, label="lbl"))
    assert res.identities_scanned == 0 and res.profiles_written == 0
    ((sql, params),) = calls
    cooldown = sql.index("updated_at > now() - toIntervalSecond({cool:UInt32})")
    assert cooldown < sql.index("GROUP BY") < sql.index("LIMIT {lim:UInt32}")
    assert "memory_key = {label:String}" in sql and "created_at" not in sql
    assert params["cool"] == 3600 and params["label"] == "lbl"


def test_the_config_reads_settings_when_built(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "profile_consolidation_max_identities_per_pass", 3)
    assert ProfileConsolidationConfig().max_identities_per_pass == 3


@pytest.mark.asyncio
async def test_default_fold_reads_stated_facts_only(monkeypatch) -> None:
    """The fold scans and reads preference/decision rows only, in one
    per-user query, and asks the model for the short stated-facts profile."""
    calls: list[tuple[str, dict]] = []
    llm_calls: list[dict] = []

    class _RecCH(StubCH):
        async def query_all(self, sql, params=None):
            calls.append((sql, dict(params or {})))
            return await super().query_all(sql, params)

    class _RecLLM:
        async def complete(self, system, user, max_tokens):
            llm_calls.append({"system": system, "user": user, "max_tokens": max_tokens})
            return _LLMOut()

    ch = _RecCH(
        identities=[{"org": "dev", "namespace": "ns", "user_id": "u1", "n": 6}],
        memories=[{"value": "vim", "since": "2025-09-01"}],
    )
    _patch(monkeypatch, ch, llm=False)
    import server.llm.client as llmmod
    import server.memory.app.profile as profmod

    monkeypatch.setattr(llmmod, "get_llm_client", lambda: _RecLLM())

    async def fake_set(**kwargs):
        return None

    monkeypatch.setattr(profmod, "write_folded_section", fake_set)
    res = await run_pass(ProfileConsolidationConfig(model=""))
    assert res.profiles_written == 1
    scan, *reads = calls
    assert scan[1]["types"] == ["preference", "decision"]
    assert len(reads) == 1 and "memory_type = 'semantic'" not in reads[0][0]
    (call,) = llm_calls
    assert call["max_tokens"] == 320
    assert call["user"] == "- [2025-09-01] vim"
    assert "EPISODES" not in call["system"]


@pytest.mark.asyncio
async def test_an_unlabelled_credential_row_never_reaches_the_fold_model(monkeypatch) -> None:
    # copy_to and imports leave a row unlabelled until the sweep; the fold's output becomes a
    # plain profile section, so the model never sees such a value.
    token = "ghp_" + "a" * 36
    sent: list[str] = []

    class _RecLLM:
        async def complete(self, system, user, max_tokens):
            sent.append(user)
            return _LLMOut()

    ch = StubCH(
        identities=[{"org": "dev", "namespace": "ns", "user_id": "u1", "n": 6}],
        memories=[
            {"value": "vim", "since": "2025-09-01"},
            {"value": f"ci uses {token}", "since": "2025-09-02"},
        ],
    )
    _patch(monkeypatch, ch, llm=False)
    import server.llm.client as llmmod
    import server.memory.app.profile as profmod

    monkeypatch.setattr(llmmod, "get_llm_client", lambda: _RecLLM())

    async def fake_set(**kwargs):
        return None

    monkeypatch.setattr(profmod, "write_folded_section", fake_set)
    await run_pass(ProfileConsolidationConfig(model=""))
    assert sent == ["- [2025-09-01] vim"]
