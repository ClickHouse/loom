"""The key axis is on, off, or decided per
namespace from key reuse; the request still wins; the probe is cached."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval import SearchQuery
from server.memory.app.retrieval import key_axis as ms
from server.memory.app.retrieval.key_axis import (
    key_axis_auto,
    key_axis_from_reuse,
    resolve_key_axis,
)
from server.settings import Settings


class _CH:
    def __init__(self, share: float | None, fail: bool = False) -> None:
        self.share, self.fail, self.calls = share, fail, 0

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return [{"reused_share": self.share}] if self.share is not None else []


def _q(ns: str = "ns", **kw: Any) -> SearchQuery:
    return SearchQuery(
        org="clickhouse",
        namespace=ns,
        scope="private",
        agent="bot",
        embedding_model="m",
        query_vec=[0.1] * 8,
        top_k=10,
        **kw,
    )


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    ms._KEY_AXIS_AUTO.clear()


def test_mode_default_and_reuse_threshold() -> None:
    assert Settings.model_fields["memory_search_key_axis"].default == "auto"
    assert key_axis_from_reuse(0.136, 0.08) and not key_axis_from_reuse(0.03, 0.08)
    # measured corpora sit at 0.064-0.075 (attribute keys, fresh writes) and
    # 0.027-0.037 (chat); the default splits them.
    thr = Settings.model_fields["memory_search_key_axis_auto_reuse_share"].default
    assert thr == 0.05
    assert key_axis_from_reuse(0.064, thr) and key_axis_from_reuse(0.069, thr)
    assert not key_axis_from_reuse(0.037, thr) and not key_axis_from_reuse(0.027, thr)


@pytest.mark.asyncio
async def test_auto_decides_per_namespace_and_caches() -> None:
    from types import SimpleNamespace

    s = SimpleNamespace(
        memory_search_key_axis="auto",
        memory_search_key_axis_auto_reuse_share=0.08,
        memory_search_key_axis_auto_ttl_seconds=600,
    )
    ch = _CH(0.136)  # support-bot-shaped keys
    assert await resolve_key_axis(_q("supportbot"), ch=ch, settings_obj=s) is True
    assert await resolve_key_axis(_q("supportbot"), ch=ch, settings_obj=s) is True
    assert ch.calls == 1  # cached
    ch2 = _CH(0.03)  # extractor keys
    assert await resolve_key_axis(_q("kcurve"), ch=ch2, settings_obj=s) is False
    assert ms.key_axis_auto_cached("clickhouse", "kcurve") == (False, 0.03)
    # the request wins over auto both ways
    assert await resolve_key_axis(_q("kcurve", key_axis=True), ch=ch2, settings_obj=s) is True
    assert await resolve_key_axis(_q("supportbot", key_axis=False), ch=ch, settings_obj=s) is False
    # on / off settings never probe
    s.memory_search_key_axis = "off"
    ch3 = _CH(0.9)
    assert await resolve_key_axis(_q("x"), ch=ch3, settings_obj=s) is False and ch3.calls == 0
    s.memory_search_key_axis = "on"
    assert await resolve_key_axis(_q("x"), ch=ch3, settings_obj=s) is True and ch3.calls == 0


@pytest.mark.asyncio
async def test_failed_probe_reads_off_and_is_cached() -> None:
    from types import SimpleNamespace

    s = SimpleNamespace(
        memory_search_key_axis="auto",
        memory_search_key_axis_auto_reuse_share=0.08,
        memory_search_key_axis_auto_ttl_seconds=600,
    )
    ch = _CH(None, fail=True)
    assert await key_axis_auto("clickhouse", "bad", ch=ch, settings_obj=s) is False
    assert await key_axis_auto("clickhouse", "bad", ch=ch, settings_obj=s) is False
    assert ch.calls == 1
