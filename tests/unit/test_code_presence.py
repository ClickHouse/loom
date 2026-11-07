"""A symbol's (or a pattern's) presence on every release branch is one call.

`code.symbol` with `presence: true` and `code.grep` with `mode: "presence"`
take `namespaces`, authorize every entry first (fail closed), run the
single-namespace op per namespace concurrently under the op deadline and
return one verdict row per namespace plus a summary. `absent` needs an
answered namespace with an index behind it; a deadline, an error or a
missing watermark is `unknown`, never `absent`. Unset, both payloads are
byte for byte the single-namespace ones.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError as PydanticValidationError

import server.auth.authz as authz
from server.code.api.models_grep import CodeGrepInput, CodeGrepOutput
from server.code.api.models_symbol import CodeSymbolInput, CodeSymbolOutput
from server.code.app import grep as grep_mod
from server.code.app import presence as cp
from server.code.app.symbol import symbol as hy
from server.code.domain.units import CodeUnit
from server.core.errors import AuthInsufficientError, ValidationError

# ---- fixtures ------------------------------------------------------------------

BRANCHES = [f"code-clickhouse-26-{i}" for i in range(1, 9)]


def _sym(**kw: Any) -> CodeSymbolInput:
    base: dict[str, Any] = dict(name="DB::StorageLoop", org="acme")
    base.update(kw)
    return CodeSymbolInput(**base)


def _grep(**kw: Any) -> CodeGrepInput:
    base: dict[str, Any] = dict(pattern="insert_deduplicate", org="acme")
    base.update(kw)
    return CodeGrepInput(**base)


def _def(ns: str) -> CodeUnit:
    return CodeUnit(
        unit_id=f"{ns}-s1",
        path=f"src/{ns}/Loop.h",
        start_line=9,
        end_line=37,
        symbol="DB::StorageLoop",
        kind="symbol",
        tier="scip",
    )


def _sym_out(ns: str, n: int) -> CodeSymbolOutput:
    return CodeSymbolOutput(definitions=[_def(ns) for _ in range(n)], trace_run_id=f"run-{ns}")


def _grep_out(ns: str, total: int, *, not_covered: bool = False) -> CodeGrepOutput:
    return CodeGrepOutput(
        matches=[],
        chunks_matched=total,
        files=[{"path": f"src/{ns}/a.cpp", "chunks": 2, "first_line": 42}] if total else [],
        files_total=total,
        not_covered=not_covered,
        trace_run_id=f"run-{ns}",
    )


class _Single:
    """Stands in for the single-namespace handler: canned outputs (or an
    exception) per namespace, optional delay, records what it was asked."""

    def __init__(self, outputs: dict[str, Any], delays: dict[str, float] | None = None) -> None:
        self.outputs = outputs
        self.delays = delays or {}
        self.calls: list[Any] = []

    async def __call__(self, inp: Any) -> Any:
        self.calls.append(inp)
        await asyncio.sleep(self.delays.get(inp.namespace, 0.0))
        out = self.outputs[inp.namespace]
        if isinstance(out, BaseException):
            raise out
        return out


@pytest.fixture
def _authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(org: str, ns: str, role: Any = None, *, hides_answer=True) -> bool:
        return True

    monkeypatch.setattr(authz, "row_in_scope", _ok)


@pytest.fixture
def as_of(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """Per-namespace provenance blocks the runner reads beside the op; a
    namespace absent here has no watermark ({}), the no-index case."""
    blocks: dict[str, dict[str, Any]] = {
        ns: {"commit": f"{ns}-head", "status": "fresh"} for ns in BRANCHES
    }

    async def _as_of(repo: Any, org: str, ns: str) -> dict[str, Any]:
        return blocks.get(ns, {})

    import server.code.app.common as cc

    monkeypatch.setattr(cc, "index_as_of", _as_of)
    return blocks


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---- wire models ---------------------------------------------------------------


def test_unset_presence_leaves_both_payloads_unchanged() -> None:
    sym = _sym_out("n", 1)
    dumped = sym.model_dump()
    assert "presence" not in dumped and "summary" not in dumped
    assert "presence" not in sym.model_dump_json()
    assert set(dumped) == {
        "definitions",
        "ambiguous",
        "candidates",
        "tokens_returned",
        "trace_run_id",
        "uses",
        "uses_truncated",
    }
    grep = _grep_out("n", 3)
    g = grep.model_dump()
    assert "presence" not in g and "summary" not in g and "presence" not in grep.model_dump_json()
    assert set(g) == {
        "matches",
        "next_cursor",
        "has_more",
        "files",
        "files_total",
        "chunks_matched",
        "truncated",
        "coverage_floor",
        "as_of",
        "not_covered",
        "note",
        "trace_run_id",
    }
    # and both keys ride once the mode fired
    fired = sym.model_copy(update={"presence": [{"namespace": "n"}], "summary": {"present": ["n"]}})
    assert "presence" in fired.model_dump() and "summary" in fired.model_dump()
    # the memo round trip reads a payload without the keys
    assert CodeGrepOutput.model_validate_json(grep.model_dump_json()).presence == []


def test_namespaces_without_the_mode_are_refused_on_both_ops() -> None:
    with pytest.raises(PydanticValidationError, match="presence: true"):
        _sym(namespaces=["a", "b"])
    with pytest.raises(PydanticValidationError, match="mode: 'presence'"):
        _grep(namespaces=["a", "b"])
    with pytest.raises(PydanticValidationError, match="cursor"):
        _grep(mode="presence", namespaces=["a"], cursor="abc")
    with pytest.raises(PydanticValidationError):
        _sym(presence=True, namespaces=[f"n{i}" for i in range(9)])


def test_namespaces_are_trimmed_deduplicated_and_led_by_namespace() -> None:
    s = _sym(presence=True, namespace="lead", namespaces=[" b ", "a", "b"])
    assert s.namespaces == ["lead", "b", "a"]
    g = _grep(mode="presence", namespace="b", namespaces=["b", "a"])
    assert g.namespaces == ["b", "a"]
    assert _grep(mode="presence").effective_limit == 1


def test_unset_presence_never_enters_the_presence_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stop(Exception):
        pass

    async def _never(*a: Any, **k: Any) -> Any:
        raise AssertionError("presence must not run for a plain request")

    def _stop(org: str, ns: str) -> tuple[str, str]:
        raise _Stop((org, ns))

    monkeypatch.setattr(cp, "symbol_presence", _never)
    monkeypatch.setattr(cp, "grep_presence", _never)
    monkeypatch.setattr(hy, "resolve_code_identity", _stop)
    monkeypatch.setattr(grep_mod, "resolve_code_identity", _stop)
    monkeypatch.setattr(hy, "require_code_intel", lambda: None)
    monkeypatch.setattr(grep_mod, "require_code_intel", lambda: None)
    with pytest.raises(_Stop) as e:
        _run(hy.handle_code_symbol(_sym(namespace="only")))
    assert e.value.args[0] == ("acme", "only")
    with pytest.raises(_Stop) as e:
        _run(grep_mod.handle_code_grep(_grep(namespace="only", mode="files")))
    assert e.value.args[0] == ("acme", "only")


def test_handlers_dispatch_the_mode_to_the_presence_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def _sym_p(inp: Any, single: Any) -> Any:
        seen.append(f"symbol:{inp.namespaces}")
        return CodeSymbolOutput(definitions=[])

    async def _grep_p(inp: Any, single: Any) -> Any:
        seen.append(f"grep:{inp.namespaces}")
        return CodeGrepOutput(matches=[], chunks_matched=0)

    monkeypatch.setattr(cp, "symbol_presence", _sym_p)
    monkeypatch.setattr(cp, "grep_presence", _grep_p)
    monkeypatch.setattr(hy, "require_code_intel", lambda: None)
    monkeypatch.setattr(grep_mod, "require_code_intel", lambda: None)
    _run(hy.handle_code_symbol(_sym(presence=True, namespaces=["a", "b"])))
    _run(grep_mod.handle_code_grep(_grep(mode="presence", namespaces=["a", "b"])))
    assert seen == ["symbol:['a', 'b']", "grep:['a', 'b']"]


# ---- authorization + cap -------------------------------------------------------


def test_one_unreadable_namespace_refuses_the_whole_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _scope(org: str, ns: str, role: Any = None, *, hides_answer=True) -> bool:
        return ns != "code-clickhouse-26-3"

    monkeypatch.setattr(authz, "row_in_scope", _scope)
    single = _Single({ns: _sym_out(ns, 1) for ns in BRANCHES})
    with pytest.raises(AuthInsufficientError) as e:
        _run(cp.symbol_presence(_sym(presence=True, namespaces=BRANCHES[:4]), single))
    assert "code.symbol" in str(e.value) and "26-3" in str(e.value)
    assert single.calls == []  # nothing ran


def test_a_one_entry_list_naming_an_unreadable_namespace_is_refused_on_both_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate checks `namespace`, not the list: a one-entry list is authorized as each
    entry of a longer one, through the handler, before anything runs."""
    checked: list[str] = []

    async def _scope(org: str, ns: str, role: Any = None, *, hides_answer=True) -> bool:
        checked.append(ns)
        return ns != "code-secret"

    monkeypatch.setattr(authz, "row_in_scope", _scope)
    monkeypatch.setattr(hy, "require_code_intel", lambda: None)
    monkeypatch.setattr(grep_mod, "require_code_intel", lambda: None)
    with pytest.raises(AuthInsufficientError, match="code.symbol.*code-secret"):
        _run(hy.handle_code_symbol(_sym(presence=True, namespaces=["code-secret"])))
    with pytest.raises(AuthInsufficientError, match="code.grep.*code-secret"):
        _run(grep_mod.handle_code_grep(_grep(mode="presence", namespaces=["code-secret"])))
    assert checked == ["code-secret", "code-secret"]


def test_the_deployment_cap_can_lower_the_wire_cap(
    monkeypatch: pytest.MonkeyPatch, _authorized: None
) -> None:
    monkeypatch.setattr(cp.settings, "code_search_fanout_max_namespaces", 2)
    with pytest.raises(ValidationError, match="cap of 2"):
        _run(cp.grep_presence(_grep(mode="presence", namespaces=BRANCHES[:3]), _Single({})))


# ---- verdicts ------------------------------------------------------------------


def test_symbol_presence_returns_one_row_per_namespace_in_order(
    _authorized: None, as_of: dict[str, dict[str, Any]]
) -> None:
    outs: dict[str, Any] = {ns: _sym_out(ns, 1) for ns in BRANCHES}
    outs["code-clickhouse-26-1"] = _sym_out("code-clickhouse-26-1", 0)  # answered empty
    outs["code-clickhouse-26-2"] = _sym_out("code-clickhouse-26-2", 2)  # two overloads
    outs["code-clickhouse-26-5"] = RuntimeError("boom")  # a failing namespace
    as_of.pop("code-clickhouse-26-6")  # no watermark
    outs["code-clickhouse-26-6"] = _sym_out("code-clickhouse-26-6", 0)
    single = _Single(outs)
    out = _run(cp.symbol_presence(_sym(presence=True, namespaces=BRANCHES), single))

    assert out.definitions == [] and out.candidates == [] and not out.ambiguous
    assert [r["namespace"] for r in out.presence] == BRANCHES
    by = {r["namespace"]: r for r in out.presence}
    assert by["code-clickhouse-26-1"]["verdict"] == "absent"
    assert by["code-clickhouse-26-1"]["indexed_commit"] == "code-clickhouse-26-1-head"
    assert by["code-clickhouse-26-2"] | {} == {
        "namespace": "code-clickhouse-26-2",
        "verdict": "present",
        "path": "src/code-clickhouse-26-2/Loop.h",
        "start_line": 9,
        "end_line": 37,
        "tier": "scip",
        "indexed_commit": "code-clickhouse-26-2-head",
        "index_status": "fresh",
        "ms": by["code-clickhouse-26-2"]["ms"],
        "reason": "",
        "definitions": 2,
    }
    assert by["code-clickhouse-26-5"]["verdict"] == "unknown"
    assert by["code-clickhouse-26-5"]["reason"] == "error:RuntimeError"
    assert by["code-clickhouse-26-6"]["verdict"] == "unknown"
    assert by["code-clickhouse-26-6"]["reason"] == "no_index"
    assert out.summary == {
        "present": [BRANCHES[i] for i in (1, 2, 3, 6, 7)],
        "absent": ["code-clickhouse-26-1"],
        "unknown": ["code-clickhouse-26-5", "code-clickhouse-26-6"],
    }
    assert out.tokens_returned > 0
    # the sub-calls are the plain lookup on each namespace, no bodies, no enrichment
    assert sorted(c.namespace for c in single.calls) == sorted(BRANCHES)
    assert all(
        not c.presence
        and c.namespaces == []
        and not c.include_body
        and not c.include_history
        and not c.include_examples
        and not c.include_lessons
        and not c.uses
        for c in single.calls
    )
    dumped = out.model_dump()
    assert "presence" in dumped and "summary" in dumped


def test_grep_presence_uses_files_mode_limit_one_and_reports_totals(
    _authorized: None, as_of: dict[str, dict[str, Any]]
) -> None:
    outs: dict[str, Any] = {
        "code-clickhouse-26-1": _grep_out("code-clickhouse-26-1", 7),
        "code-clickhouse-26-2": _grep_out("code-clickhouse-26-2", 0),
        "code-clickhouse-26-3": _grep_out("code-clickhouse-26-3", 0, not_covered=True),
    }
    single = _Single(outs)
    out = _run(cp.grep_presence(_grep(mode="presence", namespaces=BRANCHES[:3]), single))
    by = {r["namespace"]: r for r in out.presence}
    assert by["code-clickhouse-26-1"]["verdict"] == "present"
    assert by["code-clickhouse-26-1"]["files_total"] == 7
    assert by["code-clickhouse-26-1"]["path"] == "src/code-clickhouse-26-1/a.cpp"
    assert by["code-clickhouse-26-1"]["start_line"] == by["code-clickhouse-26-1"]["end_line"] == 42
    assert by["code-clickhouse-26-2"]["verdict"] == "absent"
    assert by["code-clickhouse-26-3"]["verdict"] == "unknown"
    assert by["code-clickhouse-26-3"]["reason"] == "not_covered"
    assert out.summary == {
        "present": ["code-clickhouse-26-1"],
        "absent": ["code-clickhouse-26-2"],
        "unknown": ["code-clickhouse-26-3"],
    }
    assert out.matches == [] and out.files == [] and out.files_total == 0
    assert "presence over 3" in out.note
    assert all(c.mode == "files" and c.limit == 1 and c.namespaces == [] for c in single.calls)


def test_a_scan_cut_at_its_deadline_is_unknown_never_absent(
    _authorized: None, as_of: dict[str, dict[str, Any]]
) -> None:
    """The per-namespace grep keeps the caller's deadline; a scan the budget
    cut before it reached a match read part of the index only."""
    cut = _grep_out("code-clickhouse-26-1", 0).model_copy(
        update={"partial": True, "partial_reason": "deadline: scan 120 ms"}
    )
    found = _grep_out("code-clickhouse-26-2", 3).model_copy(update={"partial": True})
    single = _Single({"code-clickhouse-26-1": cut, "code-clickhouse-26-2": found})
    out = _run(cp.grep_presence(_grep(mode="presence", namespaces=BRANCHES[:2]), single))
    by = {r["namespace"]: r for r in out.presence}
    assert by["code-clickhouse-26-1"]["verdict"] == "unknown"
    assert by["code-clickhouse-26-1"]["reason"] == "partial:deadline: scan 120 ms"
    assert by["code-clickhouse-26-2"]["verdict"] == "present"  # a hit stands


def test_a_namespace_past_the_deadline_is_unknown_never_absent(
    monkeypatch: pytest.MonkeyPatch, _authorized: None, as_of: dict[str, dict[str, Any]]
) -> None:
    monkeypatch.setattr(cp.settings, "code_search_fanout_timeout_ms", 50)
    outs = {ns: _sym_out(ns, 0) for ns in BRANCHES[:3]}
    single = _Single(outs, delays={"code-clickhouse-26-2": 5.0})
    out = _run(cp.symbol_presence(_sym(presence=True, namespaces=BRANCHES[:3]), single))
    by = {r["namespace"]: r for r in out.presence}
    assert by["code-clickhouse-26-2"]["verdict"] == "unknown"
    assert by["code-clickhouse-26-2"]["reason"] == "deadline"
    assert by["code-clickhouse-26-2"]["ms"] == 50
    assert by["code-clickhouse-26-1"]["verdict"] == "absent"
    assert out.summary["unknown"] == ["code-clickhouse-26-2"]


def test_unset_namespaces_mean_the_addressed_namespace_alone(
    _authorized: None, as_of: dict[str, dict[str, Any]]
) -> None:
    ns = "code-clickhouse-26-8"
    single = _Single({ns: _sym_out(ns, 1)})
    out = _run(cp.symbol_presence(_sym(presence=True, namespace=ns), single))
    assert [r["namespace"] for r in out.presence] == [ns]
    assert out.summary["present"] == [ns]


def test_the_span_says_presence(_authorized: None, as_of: dict[str, dict[str, Any]]) -> None:
    with patch("server.code.app.spans.set_code_span_attributes") as span:
        single = _Single({ns: _sym_out(ns, 1 if ns.endswith("8") else 0) for ns in BRANCHES[6:]})
        _run(cp.symbol_presence(_sym(presence=True, namespaces=BRANCHES[6:]), single))
    kw = span.call_args.kwargs
    assert kw["route"] == "presence" and kw["fanout"] == 2 and kw["results"] == 1
    assert kw["fanout_dropped"] == 0 and kw["retrieval_risk"] == "low"
