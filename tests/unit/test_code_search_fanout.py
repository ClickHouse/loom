"""A search over several namespaces interleaves their ranks under one budget
and one deadline.

`namespaces` on code.search runs the single-namespace search per entry,
concurrently; ranks are interleaved round-robin (scores are not comparable
across namespaces — idf is per namespace), the interleaved list is budgeted
like a single call, a namespace past the deadline is dropped and disclosed,
every namespace is authorized before anything runs (fail closed), and an
unset field leaves the handler path and the wire payload byte for byte the
single-namespace ones.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError as PydanticValidationError

import server.auth.authz as authz
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
from server.code.app import search_fanout as fo
from server.code.app.search import handler as h
from server.code.domain.units import CodeUnit, estimate_unit_tokens
from server.core.errors import AuthInsufficientError, ValidationError
from server.operations.base import Role

# ---- fixtures ------------------------------------------------------------------


def _inp(**kw: Any) -> CodeSearchInput:
    base: dict[str, Any] = dict(query="how are quotas parsed", top_k=5, org="acme")
    base.update(kw)
    return CodeSearchInput(**base)


def _unit(ns: str, i: int, *, body: str = "int f() { return 1; }") -> CodeUnit:
    return CodeUnit(
        unit_id=f"{ns}-c{i}",
        path=f"src/{ns}/f{i}.cpp",
        start_line=1,
        end_line=9,
        symbol=f"f{i}",
        kind="function",
        body=body,
        score=round(1.0 - i / 100, 4),
    )


def _out(
    ns: str, n: int, *, abstain: bool = False, route: str = "hybrid", **kw: Any
) -> CodeSearchOutput:
    base: dict[str, Any] = dict(
        results=[] if abstain else [_unit(ns, i) for i in range(n)],
        route=route,
        abstain=abstain,
        abstain_code="no_match" if abstain else "",
        abstain_reason="nothing matched" if abstain else "",
        retrieval_risk="abstain" if abstain else "low",
        retrieval_confidence=0.7,
        calibration_explanation=f"calib-{ns}",
        score_explanation=f"rrf-{ns}",
        retrieval_work={"lexical_queries": 1, "vector_queries": 1},
        as_of={"commit": f"{ns}-commit"},
        trace_run_id=f"run-{ns}",
        tokens_returned=100,
        top_similarity=0.5,
    )
    base.update(kw)
    return CodeSearchOutput(**base)


class _Single:
    """Stands in for the single-namespace handler: canned outputs per
    namespace, optional per-namespace delay, records what it was asked."""

    def __init__(
        self, outputs: dict[str, CodeSearchOutput], delays: dict[str, float] | None = None
    ) -> None:
        self.outputs = outputs
        self.delays = delays or {}
        self.calls: list[CodeSearchInput] = []

    async def __call__(self, inp: CodeSearchInput) -> CodeSearchOutput:
        self.calls.append(inp)
        await asyncio.sleep(self.delays.get(inp.namespace, 0.0))
        return self.outputs[inp.namespace]


def _fan(inp: CodeSearchInput, single: _Single) -> CodeSearchOutput:
    return asyncio.run(fo.fan_out(inp, single))


# ---- payload identity when unset -----------------------------------------------


def test_unset_namespaces_leave_the_unit_and_response_payloads_unchanged() -> None:
    unit = _unit("n", 1)
    assert "namespace" not in unit.model_dump()
    assert "namespace" in unit.model_copy(update={"namespace": "n"}).model_dump()
    out = _out("n", 2)
    dumped = out.model_dump()
    assert "fanout" not in dumped and "fanout" not in out.model_dump_json()
    assert out.model_dump(exclude={"trace_run_id"}) == {
        k: v for k, v in dumped.items() if k != "trace_run_id"
    }
    assert "fanout" in out.model_copy(update={"fanout": {"namespaces": ["a"]}}).model_dump()
    # the memo round trip reads a payload without the key
    assert CodeSearchOutput.model_validate_json(out.model_dump_json()).fanout == {}


def test_unset_namespaces_never_enter_the_fan_out_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stop(Exception):
        pass

    async def _never(*a: Any, **k: Any) -> Any:
        raise AssertionError("fan_out must not run for a single-namespace request")

    def _stop(org: str, ns: str) -> tuple[str, str]:
        raise _Stop((org, ns))

    monkeypatch.setattr(fo, "fan_out", _never)
    monkeypatch.setattr(h, "resolve_code_identity", _stop)
    with pytest.raises(_Stop) as e:
        asyncio.run(h.handle_code_search(_inp(namespace="only")))
    assert e.value.args[0] == ("acme", "only")  # the single path, on the request's namespace


def test_one_entry_is_the_plain_single_namespace_search_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Stop(Exception):
        pass

    def _stop(org: str, ns: str) -> tuple[str, str]:
        raise _Stop((org, ns))

    monkeypatch.setattr(h, "resolve_code_identity", _stop)
    with pytest.raises(_Stop) as e:
        asyncio.run(h.handle_code_search(_inp(namespaces=["solo"])))
    assert e.value.args[0] == ("acme", "solo")


def test_handler_dispatches_two_namespaces_to_the_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], Any]] = []

    async def _fake(inp: CodeSearchInput, single: Any) -> CodeSearchOutput:
        seen.append((inp.namespaces, single))
        return _out("a", 0)

    monkeypatch.setattr(fo, "fan_out", _fake)
    asyncio.run(h.handle_code_search(_inp(namespaces=["a", "b"])))
    assert seen == [(["a", "b"], h.handle_code_search)]


# ---- validation ------------------------------------------------------------------


def test_namespaces_are_trimmed_deduplicated_and_led_by_namespace() -> None:
    inp = _inp(namespace="a", namespaces=[" b ", "a", "b", "c"])
    assert inp.namespaces == ["b", "a", "c"]  # present already: not moved
    assert _inp(namespace="lead", namespaces=["x", "y"]).namespaces == ["lead", "x", "y"]
    assert _inp(namespaces=["x"]).namespaces == ["x"] and _inp().namespaces == []
    with pytest.raises(PydanticValidationError):
        _inp(namespaces=[f"n{i}" for i in range(9)])
    with pytest.raises(PydanticValidationError):
        _inp(namespaces=["x" * 129])
    with pytest.raises(PydanticValidationError):
        _inp(namespaces=["  "])


def test_fan_out_refuses_a_cursor_and_the_deployment_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    single = _Single({"a": _out("a", 2), "b": _out("b", 2), "c": _out("c", 2)})
    with pytest.raises(ValidationError, match="ONE ranked pool"):
        _fan(_inp(namespaces=["a", "b"], cursor="abc"), single)
    monkeypatch.setattr(settings, "code_search_fanout_max_namespaces", 2)
    with pytest.raises(ValidationError, match="cap of 2"):
        _fan(_inp(namespaces=["a", "b", "c"]), single)
    assert single.calls == []  # refused before any namespace ran
    assert len(_fan(_inp(namespaces=["a", "b"]), single).results) == 4


# ---- authorization ---------------------------------------------------------------


def test_one_unreadable_namespace_refuses_the_whole_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_grant(org: str, namespace: str, subject: str) -> Role | None:
        return None

    monkeypatch.setattr(authz, "_lookup_grant", _no_grant)
    single = _Single({"ns-b": _out("ns-b", 2), "ns-a": _out("ns-a", 2)})
    set_current_auth(
        AuthInfo(
            subject="bob@x",
            org="acme",
            namespaces=("ns-b",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    try:
        with pytest.raises(AuthInsufficientError, match="ns-a"):
            _fan(_inp(namespaces=["ns-b", "ns-a"]), single)
        assert single.calls == []  # fail closed: nothing ran, nothing was trimmed
    finally:
        set_current_auth(None)
    # no auth context (auth disabled): open, as everywhere else
    assert len(_fan(_inp(namespaces=["ns-b", "ns-a"]), single).results) == 4


def test_a_one_entry_list_is_authorized_before_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate checks `namespace`; a one-entry `namespaces` becomes the namespace the
    search runs on, so it is authorized first, as each entry of a longer list is."""

    async def _no_grant(org: str, namespace: str, subject: str) -> Role | None:
        return None

    monkeypatch.setattr(authz, "_lookup_grant", _no_grant)
    set_current_auth(
        AuthInfo(
            subject="bob@x",
            org="acme",
            namespaces=("code-default",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    try:
        with pytest.raises(AuthInsufficientError, match="code-secret"):
            asyncio.run(h.handle_code_search(_inp(namespaces=["code-secret"])))

        # the namespace the caller reads is searched, as the plain single search
        class _Stop(Exception):
            pass

        def _stop(org: str, ns: str) -> tuple[str, str]:
            if ns == "code-default" and org == "acme":
                raise _Stop(ns)
            return org, ns

        monkeypatch.setattr(h, "resolve_code_identity", _stop)
        with pytest.raises(_Stop, match="code-default"):
            asyncio.run(h.handle_code_search(_inp(namespaces=["code-default"])))
    finally:
        set_current_auth(None)


# ---- interleave ------------------------------------------------------------------


def test_ranks_interleave_round_robin_skipping_abstained_and_exhausted_namespaces() -> None:
    single = _Single(
        {
            "a": _out("a", 1),
            "b": _out("b", 0, abstain=True),
            "c": _out("c", 4),
        }
    )
    out = _fan(_inp(namespaces=["a", "b", "c"], top_k=4), single)
    assert [u.unit_id for u in out.results] == ["a-c0", "c-c0", "c-c1", "c-c2"]
    assert [u.namespace for u in out.results] == ["a", "c", "c", "c"]
    assert out.route == "hybrid+fanout" and not out.abstain and out.retrieval_risk == "low"
    assert out.as_of == {"commit": "a-commit"} and out.trace_run_id == "run-a"
    assert out.next_cursor == "" and not out.has_more
    assert "no continuation" in out.score_explanation
    assert out.fanout["namespaces"] == ["a", "b", "c"] and out.fanout["interleave"] == "rank"
    assert out.fanout["dropped"] == []
    per = out.fanout["per_namespace"]
    assert per["b"]["abstain"] is True and per["b"]["results"] == 0
    assert per["c"] == {
        "ms": per["c"]["ms"],
        "results": 4,
        "abstain": False,
        "route": "hybrid",
        "commit": "c-commit",
        "trace_run_id": "run-c",
    }
    assert out.retrieval_work == {
        "lexical_queries": 3,
        "vector_queries": 3,
        "fanout_namespaces": 3,
        "fanout_dropped": 0,
    }
    # the per-namespace calls carried the same request minus the list
    assert [c.namespace for c in single.calls] == ["a", "b", "c"]
    assert all(c.namespaces == [] and c.query == "how are quotas parsed" for c in single.calls)


def test_every_namespace_abstaining_abstains_with_the_first_ones_reason() -> None:
    single = _Single({"a": _out("a", 0, abstain=True), "b": _out("b", 0, abstain=True)})
    out = _fan(_inp(namespaces=["a", "b"]), single)
    assert (
        out.abstain and out.abstain_code == "no_match" and out.abstain_reason == "nothing matched"
    )
    assert out.retrieval_risk == "abstain" and out.results == []


def test_risk_is_the_best_among_namespaces_that_returned() -> None:
    single = _Single(
        {
            "a": _out("a", 2, retrieval_risk="high"),
            "b": _out("b", 2, retrieval_risk="medium"),
            "c": _out("c", 0, abstain=True),
        }
    )
    assert _fan(_inp(namespaces=["a", "b", "c"]), single).retrieval_risk == "medium"


# ---- deadline ------------------------------------------------------------------


def test_a_namespace_past_the_deadline_is_dropped_and_disclosed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_search_fanout_timeout_ms", 80)
    single = _Single({"a": _out("a", 2), "slow": _out("slow", 2)}, delays={"slow": 2.0})
    out = _fan(_inp(namespaces=["a", "slow"]), single)
    assert [u.namespace for u in out.results] == ["a", "a"]
    assert out.fanout["dropped"] == ["slow"] and "slow" not in out.fanout["per_namespace"]
    assert out.fanout["timeout_ms"] == 80
    assert (
        out.retrieval_work["fanout_dropped"] == 1
        and "dropped=['slow']" in out.calibration_explanation
    )
    assert not out.abstain


def test_every_namespace_past_the_deadline_abstains_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_search_fanout_timeout_ms", 30)
    single = _Single({"a": _out("a", 2), "b": _out("b", 2)}, delays={"a": 2.0, "b": 2.0})
    out = _fan(_inp(namespaces=["a", "b"]), single)
    assert out.abstain and out.results == [] and out.fanout["dropped"] == ["a", "b"]
    assert "deadline" in out.abstain_reason and out.route == "hybrid+fanout"


# ---- budget ------------------------------------------------------------------------


def test_the_interleaved_list_is_budgeted_like_a_single_call() -> None:
    fat = "x = 1;\n" * 400  # ~700 tokens a body
    single = _Single(
        {
            "a": _out("a", 3, results=[_unit("a", i, body=fat) for i in range(3)]),
            "b": _out("b", 3, results=[_unit("b", i, body=fat) for i in range(3)]),
        }
    )
    out = _fan(_inp(namespaces=["a", "b"], top_k=6, token_budget=1600), single)
    assert len(out.results) == 6  # every slot is represented, elided when needed
    whole = [not u.elided for u in out.results]
    assert whole[0] and not all(whole) and whole == sorted(whole, reverse=True)  # trailing elide
    assert out.tokens_returned == sum(estimate_unit_tokens(u) for u in out.results)
    assert out.tokens_returned <= 1600
    # and exactly what one namespace's call under the same budget would serve
    alone, alone_tokens = fo.budget(
        [_unit("a", i, body=fat) for i in range(6)], _inp(top_k=6, token_budget=1600)
    )
    assert alone_tokens <= 1600 and sum(not u.elided for u in alone) == sum(whole)


def test_bodies_none_keeps_cards_only() -> None:
    single = _Single({"a": _out("a", 2), "b": _out("b", 2)})
    out = _fan(_inp(namespaces=["a", "b"], bodies="none"), single)
    assert all(u.elided and u.body == "" for u in out.results)


# ---- unit tagging + span --------------------------------------------------------------


def test_served_units_carry_their_namespace_on_the_wire_and_the_span_says_fanout() -> None:
    single = _Single({"a": _out("a", 1), "b": _out("b", 1)})
    stamped: list[dict[str, Any]] = []
    with patch(
        "server.code.app.spans.set_code_span_attributes",
        side_effect=lambda **f: stamped.append(f),
    ):
        out = _fan(_inp(namespaces=["a", "b"], top_k=2), single)
    wire = out.model_dump()
    assert [u["namespace"] for u in wire["results"]] == ["a", "b"]
    assert wire["fanout"]["per_namespace"]["a"]["results"] == 1
    assert stamped and stamped[-1]["fanout"] == 2 and stamped[-1]["fanout_dropped"] == 0
