"""A namespace says what an unpinned memory.search runs.

`retrieval_defaults` on the directory row fills the knobs the caller left
unset — after a recipe, before the deployment default — and never touches a
field the caller set. The column is carried forward by every writer like
`join_policy` and reads as "no defaults" when empty.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app import namespace_retrieval_defaults as rd
from server.memory.app.search.query_plan import (
    _apply_default_search_mode,
    _apply_search_recipe,
)
from tests.unit.schema_ddl import ddl

ROOT = Path(__file__).resolve().parents[2]


def _inp(**kw: Any) -> MemorySearchInput:
    return MemorySearchInput(org="o", namespace="n", query="q", **kw)


# --- the model and its column form ---------------------------------------------------


def test_serialize_is_compact_sorted_and_empty_when_nothing_is_set() -> None:
    from server.identity.api.models import NamespaceRetrievalDefaults

    assert rd.serialize(NamespaceRetrievalDefaults()) == ""
    m = NamespaceRetrievalDefaults(top_k=8, search_mode="rrf", auto_touch=False)
    assert rd.serialize(m) == '{"auto_touch":false,"search_mode":"rrf","top_k":8}'
    assert rd.parse(rd.serialize(m)) == {"search_mode": "rrf", "top_k": 8, "auto_touch": False}


def test_parse_reads_empty_bad_json_and_rejected_values_as_no_defaults() -> None:
    assert rd.parse("") == {}
    assert rd.parse(None) == {}
    assert rd.parse("not json") == {}
    assert rd.parse("[1]") == {}
    assert rd.parse(json.dumps({"search_mode": "vibes"})) == {}
    assert rd.parse(json.dumps({"top_k": 0})) == {}
    assert rd.parse(json.dumps({"unknown": 1})) == {}


def test_model_forbids_unknown_knobs_and_bounds_the_known_ones() -> None:
    from pydantic import ValidationError

    from server.identity.api.models import NamespaceRetrievalDefaults

    with pytest.raises(ValidationError):
        NamespaceRetrievalDefaults(topk=5)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        NamespaceRetrievalDefaults(top_k=501)
    with pytest.raises(ValidationError):
        NamespaceRetrievalDefaults(reinforcement_weight=1.5)
    with pytest.raises(ValidationError):
        NamespaceRetrievalDefaults(bodies="huge")  # type: ignore[arg-type]


# --- apply: what the caller left unset ----------------------------------------------

_ALL = {
    "search_mode": "rrf",
    "top_k": 8,
    "auto_touch": False,
    "include_tags": True,
    "bodies": "excerpt",
    "reinforcement_weight": 0.0,
}


def test_every_unset_knob_takes_the_namespace_value_and_is_named() -> None:
    out = rd.apply(_inp(), _ALL)
    assert out.search_mode == "rrf"
    assert out.top_k == 8
    assert out.auto_touch is False
    assert out.include_tags is True
    assert out.bodies == "excerpt"
    assert out.reinforcement_weight == 0.0
    assert out._namespace_defaults_applied == tuple(_ALL)
    # A namespace default is still a default to the planner
    assert out._search_mode_defaulted is True


def test_a_pinned_field_is_never_overwritten_even_when_equal_to_the_wire_default() -> None:
    pinned = _inp(
        search_mode="semantic", top_k=5, auto_touch=True, include_tags=False, bodies="full"
    )
    out = rd.apply(pinned, _ALL)
    assert out.search_mode == "semantic"
    assert out.top_k == 5
    assert out.auto_touch is True
    assert out.include_tags is False
    assert out.bodies == "full"
    # the one unset knob still applies
    assert out.reinforcement_weight == 0.0
    assert out._namespace_defaults_applied == ("reinforcement_weight",)
    assert out._search_mode_defaulted is False


def test_no_defaults_returns_the_same_object() -> None:
    inp = _inp()
    assert rd.apply(inp, {}) is inp
    pinned = _inp(search_mode="rrf")
    assert rd.apply(pinned, {"search_mode": "semantic"}) is pinned
    assert pinned._namespace_defaults_applied == ()


def test_a_recipe_owns_the_mode_but_not_the_other_knobs() -> None:
    inp = _apply_search_recipe(_inp(recipe="quality"))
    out = rd.apply(inp, _ALL)
    assert out.search_mode == "bm25_hybrid"
    assert out.top_k == 8 and out.auto_touch is False
    assert "search_mode" not in out._namespace_defaults_applied


def test_the_deployment_default_yields_to_the_namespace_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "memory_search_default_mode", "semantic")
    out = _apply_default_search_mode(rd.apply(_inp(), {"search_mode": "rrf"}))
    assert out.search_mode == "rrf"
    assert out._search_mode_defaulted is True
    # …and without a namespace default the deployment's still fills the mode
    out = _apply_default_search_mode(rd.apply(_inp(), {"top_k": 8}))
    assert out.search_mode == "semantic" and out.top_k == 8


def test_the_plan_names_the_applied_knobs_and_still_routes_identifiers() -> None:
    from server.memory.domain.query_planner import plan_memory_search

    plan = plan_memory_search(
        current_search_mode="rrf",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_text="deploy window",
        mode_pinned=False,
        namespace_defaults=("search_mode", "top_k"),
    )
    assert plan.search_mode == "rrf"
    assert plan.explain().endswith("reason=default_rrf, ns_defaults=search_mode,top_k")
    # an identifier on a namespace-defaulted semantic mode
    plan = plan_memory_search(
        current_search_mode="semantic",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_text="find cd613e30-9f2a-4b7e-8c1d-0a1b2c3d4e5f",
        mode_pinned=False,
        namespace_defaults=("search_mode",),
    )
    assert plan.search_mode == "bm25_hybrid" and plan.reason == "identifier"
    # no defaults: the explain string is byte-identical to before
    plan = plan_memory_search(
        current_search_mode="semantic",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_text="deploy window",
    )
    assert "ns_defaults" not in plan.explain()


# --- resolve: cached, fail-open ------------------------------------------------------


class _CH:
    def __init__(self, row: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.row, self.fail, self.calls = row, fail, 0

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.calls += 1
        assert "retrieval_defaults" in sql and "loom.namespaces FINAL" in sql
        if self.fail:
            raise RuntimeError("Missing columns: 'retrieval_defaults'")
        return self.row


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    rd.clear_cache()
    yield
    rd.clear_cache()


@pytest.mark.asyncio
async def test_resolve_reads_once_per_ttl_and_register_refreshes_it() -> None:
    ch = _CH({"retrieval_defaults": '{"search_mode":"rrf"}'})
    assert await rd.resolve("o", "n", ch=ch) == {"search_mode": "rrf"}
    assert await rd.resolve("o", "n", ch=ch) == {"search_mode": "rrf"}
    assert ch.calls == 1
    rd.note_registered("o", "n", {"retrieval_defaults": '{"top_k":8}'})
    assert await rd.resolve("o", "n", ch=ch) == {"top_k": 8}
    assert ch.calls == 1
    rd.invalidate("o", "n")
    await rd.resolve("o", "n", ch=ch)
    assert ch.calls == 2


@pytest.mark.asyncio
async def test_resolve_fails_open_to_no_defaults() -> None:
    ch = _CH(fail=True)
    assert await rd.resolve("o", "n", ch=ch) == {}
    assert await rd.resolve("o", "", ch=ch) == {}
    ch2 = _CH(None)
    assert await rd.resolve("o", "fresh", ch=ch2) == {}


# --- the directory row ---------------------------------------------------------------


def test_registry_row_serialises_clears_and_carries_forward() -> None:
    import server.operations  # noqa: F401 — wires the handler modules
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    now = datetime(2026, 9, 16, 8, 0, 0)
    base = {"org": "o", "namespace": "n", "owner_email": "x@y"}
    fresh = registry_row(NamespaceRegisterInput(**base), None, now)
    assert fresh["retrieval_defaults"] == ""
    kept = registry_row(
        NamespaceRegisterInput(**base),
        {"retrieval_defaults": '{"search_mode":"rrf"}', "conversation_logging_enabled": False},
        now,
    )
    assert kept["retrieval_defaults"] == '{"search_mode":"rrf"}'
    set_ = registry_row(
        NamespaceRegisterInput(**base, retrieval_defaults={"search_mode": "rrf", "top_k": 8}),
        {"retrieval_defaults": '{"auto_touch":false}', "conversation_logging_enabled": False},
        now,
    )
    assert set_["retrieval_defaults"] == '{"search_mode":"rrf","top_k":8}'
    cleared = registry_row(
        NamespaceRegisterInput(**base, retrieval_defaults={}),
        {"retrieval_defaults": '{"auto_touch":false}', "conversation_logging_enabled": False},
        now,
    )
    assert cleared["retrieval_defaults"] == ""


def test_namespace_get_reads_the_column() -> None:
    src = (ROOT / "server" / "identity" / "app" / "namespace.py").read_text()
    get = src.split("async def handle_namespace_get", 1)[1].split(
        "async def handle_namespace_delete", 1
    )[0]
    assert "retrieval_defaults" in get


def test_the_schema_declares_the_column() -> None:
    assert "`retrieval_defaults` String DEFAULT ''" in ddl("namespaces")


def test_get_output_omits_the_field_value_when_the_namespace_has_none() -> None:
    from server.identity.api.models import NamespaceGetOutput

    out = NamespaceGetOutput(found=True, namespace="n")
    assert out.retrieval_defaults is None
    assert rd.parse("") or None is None
