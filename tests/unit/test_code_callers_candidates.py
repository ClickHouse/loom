"""On request, code.callers returns the call sites the fast
tier declined as ranked candidates apart from edges, and a virtual method's
callers include callers of its overrides (tagged via_override). Off, the
payload is byte for byte the plain one."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from server.settings import settings


def _sym(
    name: str, sid: str, path: str = "src/A.cpp", start: int = 10, end: int = 40
) -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "name": name.split("::")[-1],
        "kind": "function",
        "path": path,
        "start_line": start,
        "end_line": end,
        "tier": "scip",
        "signature": "",
        "doc_comment": "",
    }


def _edge(
    frm: str,
    to: str,
    fq: str,
    tq: str,
    *,
    kind: str = "calls",
    path: str = "src/X.cpp",
    line: int = 5,
) -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": fq,
        "to_qualified_name": tq,
        "path": path,
        "line": line,
        "tier": "scip",
        "edge_kind": kind,
        "confidence": 1.0,
    }


def _miss(
    path: str,
    line: int,
    caller: str,
    *,
    text: str = "insert",
    disposition: str = "ambiguous",
    candidates: int = 4,
) -> dict[str, Any]:
    return {
        "callee_name": text.split("::")[-1],
        "path": path,
        "line": line,
        "from_symbol_id": "f-" + caller,
        "from_qualified_name": caller,
        "call_text": text,
        "disposition": disposition,
        "candidates": candidates,
    }


class _Repo:
    def __init__(self) -> None:
        self.symbols = {"DB::A::insert": _sym("DB::A::insert", "sA")}
        # callers: sX calls the seed directly; sY calls the override sO; sZ calls the base sB
        self.callers: dict[str, list[dict[str, Any]]] = {
            "sA": [_edge("sX", "sA", "DB::X::x", "DB::A::insert")],
            "sO": [_edge("sY", "sO", "DB::Y::y", "DB::AImpl::insert", path="src/Y.cpp", line=9)],
            "sB": [_edge("sZ", "sB", "DB::Z::z", "DB::IBase::insert", path="src/Z.cpp", line=3)],
        }
        # overrides edges point from the override to the base
        self.overrides_of: dict[str, list[dict[str, Any]]] = {}
        self.misses: list[dict[str, Any]] = []
        self.path_misses: dict[str, list[dict[str, Any]]] = {}
        self.miss_reads: list[tuple[list[str], int]] = []
        self.edge_reads: list[tuple[str, list[str] | None, list[str]]] = []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        s = self.symbols.get(name)
        return [s] if s else []

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols.values() if s["symbol_id"] in ids]

    async def edges_for_symbols(
        self,
        org,
        ns,
        ids,
        *,
        direction,
        edge_kinds=None,
        limit=200,
        min_confidence=0.0,
        per_match=None,
    ):
        self.edge_reads.append((direction, edge_kinds, list(ids)))
        if direction != "callers":
            return []
        if edge_kinds == ["overrides"]:
            return [e for sid in ids for e in self.overrides_of.get(sid, [])][:limit]
        return [e for sid in ids for e in self.callers.get(sid, [])]

    async def call_misses_for_names(self, org, ns, names):
        amb = sum(1 for m in self.misses if m["disposition"] == "ambiguous")
        unr = len(self.misses) - amb
        return {"insert": {"unresolved": unr, "ambiguous": amb}} if self.misses else {}

    async def live_call_misses_for_names(self, org, ns, names, *, limit=2000):
        self.miss_reads.append((list(names), limit))
        return [m for m in self.misses if m["callee_name"] in names][:limit]

    async def live_call_misses_for_path(self, org, ns, path):
        return list(self.path_misses.get(path, []))

    async def scip_covered_paths(self, org, ns, paths):
        return set(paths)

    async def scip_stale_paths(self, org, ns, paths):
        return {p for p in paths if p in getattr(self, "stale_paths", set())}


@pytest.fixture
def intel(monkeypatch: pytest.MonkeyPatch):
    import server.code.app.symbol.graph as h

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)

    async def no_dispatch(*a, **k):
        return []

    monkeypatch.setattr(h, "_dispatch_seeds", no_dispatch)
    return h


async def _callers(intel, repo: _Repo, **fields: Any):
    from server.code.api.models_symbol import CodeCallersInput

    with patch.object(intel, "CodeRepo", return_value=repo):
        return await intel.handle_code_callers(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert", **fields)
        )


def _seed_misses() -> list[dict[str, Any]]:
    return [
        _miss(
            "tests/integration/test_x/test.py", 12, "tests.x::test", candidates=195
        ),  # python: dropped
        _miss("src/Other.cpp", 20, "DB::Other::run", text="Bar::insert"),  # other scope: dropped
        _miss("src/X.cpp", 5, "DB::X::x"),  # already an edge
        _miss("src/P.cpp", 30, "DB::P::p", candidates=4),  # 0.25
        _miss("src/Q.cpp", 31, "DB::Q::q", text="A::insert", candidates=2),  # receiver scope A, 0.5
        _miss("src/R.cpp", 32, "DB::R::r", disposition="unresolved", candidates=0),  # 0.5
        _miss("src/P.cpp", 33, "DB::P::p2", candidates=40),  # 0.1 floor
    ]


async def test_flag_off_is_the_payload_it_was(intel) -> None:
    repo = _Repo()
    repo.misses = _seed_misses()
    out = await _callers(intel, repo, include_ambiguous=False)
    data = out.model_dump(mode="json")
    assert "candidate_sites" not in data
    assert all("via_override" not in e for e in data["edges"])
    assert set(data) == {
        "edges",
        "seed_symbols",
        "ambiguous",
        "candidates",
        "dispatch_seeds",
        "note",
        "truncated",
        "counts_floor",
        "unresolved_calls",
        "ambiguous_calls",
        "epistemic",
        "causes",
        "by_hop_counts",
        "trace_run_id",
    }
    # the declined-site read and the overrides read did not run
    assert repo.miss_reads == []
    assert all(kinds != ["overrides"] for _, kinds, _ in repo.edge_reads)
    assert out.ambiguous_calls == 6 and out.unresolved_calls == 1


async def test_flag_on_returns_filtered_ranked_candidates_apart_from_edges(intel) -> None:
    repo = _Repo()
    repo.misses = _seed_misses()
    out = await _callers(intel, repo, include_ambiguous=True, candidate_min_confidence=0.0)
    # edges are untouched: nothing merged
    assert [e.from_symbol for e in out.edges] == ["DB::X::x"]
    assert out.epistemic == "lower_bound"
    block = out.candidate_sites
    assert block is not None and block.epistemic == "lower_bound"
    assert repo.miss_reads == [(["insert"], 2000)]
    assert (
        block.total == 4 and block.returned == 4 and not block.truncated and not block.read_capped
    )
    assert (
        "dropped 1 in another language, 1 written with another scope, 1 already in edges"
        in block.note
    )
    assert block.by_reason == {"ambiguous_overload": 3, "unresolved_name": 1}
    # groups: written scope first at 0.5, then files by best confidence
    assert [(g.kind, g.key, g.n, g.confidence) for g in block.groups] == [
        ("receiver_scope", "A", 1, 0.5),
        ("file", "src/R.cpp", 1, 0.5),
        ("file", "src/P.cpp", 2, 0.25),
    ]
    p = next(g for g in block.groups if g.key == "src/P.cpp")
    assert [(s.line, s.caller, s.confidence, s.reason) for s in p.sites] == [
        (30, "DB::P::p", 0.25, "ambiguous_overload"),
        (33, "DB::P::p2", 0.1, "ambiguous_overload"),
    ]
    r = next(g for g in block.groups if g.key == "src/R.cpp")
    assert r.sites[0].reason == "unresolved_name" and r.sites[0].disposition == "unresolved"
    data = out.model_dump(mode="json")
    assert data["candidate_sites"]["groups"][0]["sites"][0]["path"] == "src/Q.cpp"
    # the ambiguity-guard field is untouched by the block
    assert data["candidates"] == [] and data["ambiguous"] is False


async def test_base_seed_walks_its_overrides_and_tags_the_edges(intel) -> None:
    repo = _Repo()
    repo.misses = _seed_misses()
    repo.overrides_of["sA"] = [
        _edge(
            "sO",
            "sA",
            "DB::AImpl::insert",
            "DB::A::insert",
            kind="overrides",
            path="src/AImpl.cpp",
            line=1,
        ),
    ]
    out = await _callers(intel, repo, include_ambiguous=True, candidate_min_confidence=0.0)
    by_from = {e.from_symbol: e for e in out.edges}
    assert set(by_from) == {"DB::X::x", "DB::Y::y"}
    assert by_from["DB::X::x"].via_override == ""
    assert by_from["DB::Y::y"].via_override == "DB::AImpl::insert"
    assert {"symbol": "DB::AImpl::insert", "via": "overridden_by"} in out.dispatch_seeds
    assert "callers of 1 override(s) of the seed are included, tagged via_override" in out.note
    # a virtual seed: its ambiguous sites are virtual dispatch, not overloads
    assert out.candidate_sites is not None
    assert out.candidate_sites.by_reason == {"unresolved_name": 1, "virtual_dispatch": 3}
    data = out.model_dump(mode="json")
    tagged = [e for e in data["edges"] if e["from_symbol"] == "DB::Y::y"][0]
    assert tagged["via_override"] == "DB::AImpl::insert"
    assert "via_override" not in [e for e in data["edges"] if e["from_symbol"] == "DB::X::x"][0]


async def test_override_seed_tags_the_base_callers(intel, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo()

    async def base(*a, **k):
        return [
            {
                "symbol_id": "sB",
                "qualified_name": "DB::IBase::insert",
                "path": "src/IBase.h",
                "via": "overrides",
            }
        ]

    monkeypatch.setattr(intel, "_dispatch_seeds", base)
    off = await _callers(intel, repo, include_ambiguous=False)
    assert {e.from_symbol: e.via_override for e in off.edges} == {"DB::X::x": "", "DB::Z::z": ""}
    on = await _callers(intel, repo, include_ambiguous=True, candidate_min_confidence=0.0)
    assert {e.from_symbol: e.via_override for e in on.edges} == {
        "DB::X::x": "",
        "DB::Z::z": "DB::IBase::insert",
    }


async def test_override_fanout_is_capped_and_disclosed(intel) -> None:
    from server.code.app.candidates import OVERRIDE_FANOUT_CAP

    repo = _Repo()
    repo.overrides_of["sA"] = [
        _edge(f"sO{i}", "sA", f"DB::Impl{i}::insert", "DB::A::insert", kind="overrides", line=i)
        for i in range(OVERRIDE_FANOUT_CAP + 5)
    ]
    out = await _callers(intel, repo, include_ambiguous=True, candidate_min_confidence=0.0)
    walked = [d for d in out.dispatch_seeds if d["via"] == "overridden_by"]
    assert len(walked) == OVERRIDE_FANOUT_CAP
    assert f"first {OVERRIDE_FANOUT_CAP} of more; the rest are not walked" in out.note


async def test_max_nodes_bounds_the_candidate_block_and_says_so(intel) -> None:
    repo = _Repo()
    repo.misses = [_miss(f"src/F{i}.cpp", i, f"DB::F{i}::f", candidates=2 + i) for i in range(12)]
    out = await _callers(
        intel, repo, include_ambiguous=True, candidate_min_confidence=0.0, max_nodes=5
    )
    block = out.candidate_sites
    assert block is not None
    assert block.total == 12 and block.returned == 5 and block.truncated
    assert sum(g.n for g in block.groups) == 5
    # highest confidence first: the smallest candidate sets
    assert [s.line for g in block.groups for s in g.sites] == [0, 1, 2, 3, 4]
    assert "12 candidate site(s) passed the filters, 5 returned (max_nodes)" in block.note


async def test_read_cap_is_disclosed_as_a_floor(intel, monkeypatch: pytest.MonkeyPatch) -> None:
    import server.code.app.candidates as c

    monkeypatch.setattr(c, "CANDIDATE_READ_CAP", 3)
    repo = _Repo()
    repo.misses = [_miss(f"src/F{i}.cpp", i, f"DB::F{i}::f") for i in range(10)]
    out = await _callers(intel, repo, include_ambiguous=True, candidate_min_confidence=0.0)
    assert repo.miss_reads == [(["insert"], 3)]
    assert out.candidate_sites is not None and out.candidate_sites.read_capped
    assert "stopped at 3 rows; `total` is a floor" in out.candidate_sites.note


async def test_candidate_read_failure_never_fails_the_answer(intel) -> None:
    repo = _Repo()

    async def boom(org, ns, names, *, limit=2000):
        raise RuntimeError("table missing")

    repo.live_call_misses_for_names = boom  # type: ignore[method-assign]
    out = await _callers(intel, repo, include_ambiguous=True, candidate_min_confidence=0.0)
    assert len(out.edges) == 1
    assert out.candidate_sites is not None and out.candidate_sites.total == 0
    assert "candidate read failed" in out.candidate_sites.note


async def test_callees_return_the_seeds_own_declined_sites(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    repo = _Repo()
    repo.path_misses["src/A.cpp"] = [
        _miss("src/A.cpp", 12, "DB::A::insert", text="parse", candidates=5),
        _miss(
            "src/A.cpp",
            15,
            "DB::A::insert",
            text="Poco::Logger::get",
            disposition="unresolved",
            candidates=0,
        ),
        _miss("src/A.cpp", 90, "DB::A::other", text="parse", candidates=5),  # outside the seed span
    ]
    with patch.object(intel, "CodeRepo", return_value=repo):
        off = await intel.handle_code_callees(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert", include_ambiguous=False)
        )
        on = await intel.handle_code_callees(
            CodeCallersInput(
                org="o",
                namespace="n",
                name="DB::A::insert",
                include_ambiguous=True,
                candidate_min_confidence=0.0,
            )
        )
    assert "candidate_sites" not in off.model_dump(mode="json")
    block = on.candidate_sites
    assert block is not None and block.total == 2
    assert [(g.kind, g.key, g.confidence) for g in block.groups] == [
        ("callee", "get", 0.5),
        ("callee", "parse", 0.2),
    ]
    assert block.groups[0].sites[0].callee_text == "Poco::Logger::get"
    # no overrides read in the callees direction
    assert all(kinds != ["overrides"] for _, kinds, _ in repo.edge_reads)


def test_confidence_and_reason_rules() -> None:
    from server.code.app.candidates import confidence_of, reason_of

    assert confidence_of("ambiguous", 1) == 1.0
    assert confidence_of("ambiguous", 4) == 0.25
    assert confidence_of("ambiguous", 400) == 0.1
    assert confidence_of("ambiguous", 0) == 1.0
    assert confidence_of("unresolved", 0) == 0.5
    assert reason_of("unresolved", virtual=True) == "unresolved_name"
    assert reason_of("ambiguous", virtual=True) == "virtual_dispatch"
    assert reason_of("ambiguous", virtual=False) == "ambiguous_overload"


def test_edge_wire_drops_the_empty_tag() -> None:
    from server.code.api.models_symbol import CodeEdgeWire

    e = CodeEdgeWire(
        edge_kind="calls",
        from_symbol="a",
        to_symbol="b",
        from_symbol_id="1",
        to_symbol_id="2",
        path="p",
        line=1,
        tier="scip",
        confidence=1.0,
    )
    assert "via_override" not in e.model_dump(mode="json")
    assert (
        e.model_copy(update={"via_override": "B::b"}).model_dump(mode="json")["via_override"]
        == "B::b"
    )


async def test_sdk_sends_the_flag_only_when_set() -> None:
    from sdk.python.loom.code import CodeClient

    posted: list[tuple[str, dict[str, Any]]] = []

    class _T:
        async def post(self, path, body):
            posted.append((path, body))
            return {}

    class _A:
        transport = _T()

    client = CodeClient(_A())  # type: ignore[arg-type]
    await client.callers("X::y")
    await client.callers("X::y", include_ambiguous=True, candidate_min_confidence=0.0)
    await client.callees("X::y", include_ambiguous=True, candidate_min_confidence=0.0)
    assert "include_ambiguous" not in posted[0][1]
    assert posted[1][1]["include_ambiguous"] is True and posted[1][0] == "/v1/code.callers"
    assert posted[2][1]["include_ambiguous"] is True and posted[2][0] == "/v1/code.callees"
