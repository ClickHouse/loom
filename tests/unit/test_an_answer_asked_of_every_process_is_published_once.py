"""An answer asked of every process is published once.

The repository listing's status, the taxonomy and the tracker source views are
shared across processes: a per-process memo would leave a multi-process web
tier answering the dashboard cold on most calls. Each reads the row another
process published within the TTL before computing, and publishes what it
computed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.code.api.models_status import CodeIndexStatusOutput
from server.code.app import index_status
from server.identity.api.models_namespace_catalog import NamespaceTaxonomyInput
from server.identity.app import namespace_catalog as namespace
from server.tracker.app import sources as tracker_sources


def _status_row(age_s: float, value: str | None = None) -> dict[str, Any]:
    st = CodeIndexStatusOutput(org="acme", namespace="code-x", repo="acme/x", branch="main")
    return {
        index_status.STATUS_SNAPSHOT_KEY: {
            "value": value if value is not None else st.model_dump_json(),
            "updated_at": datetime.now(UTC) - timedelta(seconds=age_s),
        }
    }


def test_a_fresh_published_status_is_used_and_a_stale_or_broken_one_is_not() -> None:
    fresh = index_status.published_status_snapshot(_status_row(5.0))
    assert fresh is not None and fresh.namespace == "code-x"
    assert index_status.published_status_snapshot(_status_row(45.0)) is None, (
        "older than the 30 s TTL"
    )
    assert index_status.published_status_snapshot(_status_row(5.0, value="{not json")) is None
    assert index_status.published_status_snapshot({}) is None
    assert index_status.published_status_snapshot(None) is None


@pytest.mark.asyncio
async def test_the_listing_reads_a_published_status_before_computing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_status.reset_repo_status_cache()
    computed: list[str] = []

    async def _boom(cfg: Any, *, state: Any = None) -> CodeIndexStatusOutput:
        computed.append(cfg.namespace)
        return CodeIndexStatusOutput(
            org="acme", namespace=cfg.namespace, repo="acme/x", branch="main"
        )

    monkeypatch.setattr(index_status, "_compute_repo_status", _boom)

    class _Cfg:
        org = "acme"
        namespace = "code-x"

    out = await index_status._repo_status_cached(_Cfg(), state=_status_row(3.0), listing=True)
    assert out.namespace == "code-x" and computed == [], (
        "the published row answered; nothing was computed"
    )
    assert ("acme", "code-x") not in index_status._status_cache, (
        "a listing-grade row is never memoized"
    )
    index_status.reset_repo_status_cache()
    await index_status._repo_status_cached(_Cfg(), state=_status_row(90.0), listing=True)
    assert computed == ["code-x"], "a stale row is computed past"
    index_status.reset_repo_status_cache()
    await index_status._repo_status_cached(_Cfg(), state=_status_row(3.0))
    assert computed == ["code-x", "code-x"], "code.index_status never reads the listing-grade row"


def test_the_published_snapshot_is_slim_and_never_embeds_itself() -> None:
    st = CodeIndexStatusOutput(
        org="acme",
        namespace="code-x",
        repo="acme/x",
        branch="main",
        ops=["code.search"] * 200,
        state={
            "git_head": {"value": "abc", "detail": "", "updated_at": "2026-09-19 00:00:00"},
            index_status.STATUS_SNAPSHOT_KEY: {
                "value": "{" + "x" * 5000 + "}",
                "detail": "",
                "updated_at": "",
            },
            "jit_risk_fit": {"value": "y" * 4000, "detail": "", "updated_at": ""},
            "coverage": {"value": "{}", "detail": "", "updated_at": ""},
        },
    )
    raw = index_status.slim_status_snapshot_json(st)
    assert len(raw) < 4000, len(raw)
    back = CodeIndexStatusOutput.model_validate_json(raw)
    assert back.repo == "acme/x" and back.ops == []
    assert set(back.state) == {"git_head"}, (
        "only the keys a listing row reads stay in the state map (cut 64)"
    )


def test_taxonomy_state_key_names_the_version_and_the_request_shape() -> None:
    inp = NamespaceTaxonomyInput(
        org="acme", namespace="team", top_n_tags=10, top_n_entities=20, top_n_communities=5
    )
    assert namespace.taxonomy_state_key(inp, 1234) == "taxonomy@1234:10:20:5"


@pytest.mark.asyncio
async def test_tracker_views_read_the_published_row_first(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {
        tracker_sources.FRESHNESS_SNAPSHOT_KEY: {
            "value": '{"items": 7}',
            "updated_at": datetime.now(UTC),
        }
    }

    class _Repo:
        async def all_state(self, org: str, ns: str) -> dict[str, Any]:
            return rows

        async def set_state(self, *a: Any, **k: Any) -> None:
            raise AssertionError("nothing to publish when the row answered")

    import server.code.store.index as code_repo

    monkeypatch.setattr(code_repo, "CodeRepo", lambda: _Repo())
    assert await tracker_sources._freshness("acme", "tracker-x") == {"items": 7}
    rows[tracker_sources.FRESHNESS_SNAPSHOT_KEY]["updated_at"] = datetime.now(UTC) - timedelta(
        minutes=5
    )
    assert (
        await tracker_sources._published(
            "acme", "tracker-x", tracker_sources.FRESHNESS_SNAPSHOT_KEY
        )
        is None
    ), "a stale row is not an answer"
