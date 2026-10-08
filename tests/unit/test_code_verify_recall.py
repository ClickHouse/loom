"""code.verify recall + refuted semantics (file-local statics, template members,
lambdas and file:line subjects all resolve), and the pack's co-change gap
section."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from server.code.app.index.packs import render_co_change_gaps
from server.code.app.symbol import verify as m
from server.code.store.index import CodeRepo
from server.settings import settings

_SPEC = importlib.util.spec_from_file_location(
    "scip_to_delta2", Path(__file__).resolve().parents[2] / "scripts/scip_harness/scip_to_delta.py"
)
conv = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["scip_to_delta2"] = conv
_SPEC.loader.exec_module(conv)


def test_normalize_claim_name_strips_templates_and_parens() -> None:
    f = m._normalize_claim_name
    assert f("BaseSettings<TTraits>::write(WriteBuffer &)") == "BaseSettings::write"
    assert f("std::vector<std::pair<int, int>>::push_back") == "std::vector::push_back"
    assert f("DB::IStorage::read") == "DB::IStorage::read"
    assert f(" helper() ") == "helper"


def test_converter_keeps_file_local_symbols_scoped_by_path() -> None:
    p = conv._parse_symbol("cxx . . $ `<file>/src/Core/Settings.cpp`/maskSecret(deadbeef).")
    assert p is not None
    qname, kind, sig = p
    assert qname == "src/Core/Settings.cpp::maskSecret" and kind == "function"
    assert sig == "(scip:deadbeef)"
    # the file pseudo-symbol on its own is still skipped
    assert conv._parse_symbol("cxx . . $ `<file>/src/Core/Settings.cpp`/") is None
    # ordinary symbols unchanged
    assert conv._parse_symbol("cxx . . $ DB/IStorage#read(a8).")[0] == "DB::IStorage::read"


class _Repo:
    def __init__(self) -> None:
        self.bodies = {"caller": "void f() { helper(); other(); }", "quiet": "void g() { return; }"}

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        assert paths == ["src/a.cpp"]
        return [
            {
                "symbol_id": "outer",
                "qualified_name": "DB::outer",
                "path": "src/a.cpp",
                "start_line": 10,
                "end_line": 80,
                "tier": "scip",
            },
            {
                "symbol_id": "inner",
                "qualified_name": "DB::inner",
                "path": "src/a.cpp",
                "start_line": 40,
                "end_line": 50,
                "tier": "scip",
            },
        ]

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        table = {
            "DB::Caller::f": [
                {
                    "symbol_id": "caller",
                    "qualified_name": "DB::Caller::f",
                    "path": "src/c.cpp",
                    "tier": "scip",
                    "kind": "function",
                }
            ],
            "DB::Quiet::g": [
                {
                    "symbol_id": "quiet",
                    "qualified_name": "DB::Quiet::g",
                    "path": "src/q.cpp",
                    "tier": "scip",
                    "kind": "function",
                }
            ],
            "DB::helper": [
                {
                    "symbol_id": "helper",
                    "qualified_name": "DB::helper",
                    "path": "src/h.cpp",
                    "tier": "scip",
                    "kind": "function",
                }
            ],
            "DB::Foo::bar": [
                {
                    "symbol_id": "bar",
                    "qualified_name": "DB::Foo::bar",
                    "path": "src/f.cpp",
                    "tier": "scip",
                    "kind": "function",
                }
            ],
        }
        if name == "g":  # bare-name suffix match, like the real symbols_by_name
            return table["DB::Quiet::g"]
        return table.get(name, [])

    async def symbols_by_ids(self, org, ns, ids):
        return []

    async def symbols_fuzzy(self, org, ns, name):
        return []

    async def edges_for_symbols(self, org, ns, ids, *, direction, edge_kinds=None, limit=200):
        return []  # no edges recorded at all


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "clickhouse")
    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")
    monkeypatch.setattr(m, "CodeRepo", _Repo)

    async def _body(repo, org, ns, sym):
        return repo.bodies.get(str(sym["symbol_id"]), "")

    async def _complete(repo, org, ns, seeds):
        return True

    monkeypatch.setattr(m, "symbol_body", _body)
    monkeypatch.setattr(m, "_body_complete", _complete)


@pytest.mark.asyncio
async def test_file_line_subject_resolves_to_innermost_enclosing_function() -> None:
    rows = await m._resolve_claim_symbol(_Repo(), "o", "n", "src/a.cpp:45")  # type: ignore[arg-type]
    assert [r["qualified_name"] for r in rows] == ["DB::inner", "DB::outer"]
    rows = await m._resolve_claim_symbol(_Repo(), "o", "n", "src/a.cpp:20")  # type: ignore[arg-type]
    assert [r["qualified_name"] for r in rows] == ["DB::outer"]


@pytest.mark.asyncio
async def test_template_args_in_claim_still_resolve() -> None:
    rows = await m._resolve_claim_symbol(_Repo(), "o", "n", "DB::Foo<T>::bar(int)")  # type: ignore[arg-type]
    assert rows and rows[0]["qualified_name"] == "DB::Foo::bar"


@pytest.mark.asyncio
async def test_calls_refuted_when_no_edge_and_no_mention() -> None:
    from server.code.api.models_verify import CodeVerifyInput

    out = await m.handle_code_verify(
        CodeVerifyInput(kind="calls", subject="DB::Quiet::g", object="DB::helper")
    )
    assert out.verdict == "refuted" and out.reason == "no_edge_no_mention"
    assert "does not appear" in out.explanation
    assert out.evidence and out.evidence[0]["subject"] == "DB::Quiet::g"


@pytest.mark.asyncio
async def test_calls_not_established_when_name_mentioned_but_no_edge() -> None:
    from server.code.api.models_verify import CodeVerifyInput

    out = await m.handle_code_verify(
        CodeVerifyInput(kind="calls", subject="DB::Caller::f", object="DB::helper")
    )
    assert out.verdict == "not_established" and out.reason == "no_edge_name_mentioned"
    assert "appears in the subject's text" in out.explanation


@pytest.mark.asyncio
async def test_calls_incomplete_body_never_refutes(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_verify import CodeVerifyInput

    async def _incomplete(repo, org, ns, seeds):
        return False

    monkeypatch.setattr(m, "_body_complete", _incomplete)
    out = await m.handle_code_verify(
        CodeVerifyInput(kind="calls", subject="DB::Quiet::g", object="DB::helper")
    )
    assert out.verdict == "not_established" and out.reason == "no_edge_body_incomplete"


@pytest.mark.asyncio
async def test_calls_refutation_is_direct_only_and_needs_a_qualified_subject() -> None:
    from server.code.api.models_verify import CodeVerifyInput

    out = await m.handle_code_verify(
        CodeVerifyInput(kind="calls", subject="DB::Quiet::g", object="DB::helper", depth=2)
    )
    assert out.verdict == "refuted" and "directly" in out.explanation
    assert "remains unverified" in out.explanation  # the transitive part is not claimed
    # bare subject: resolves (to DB::Quiet::g via suffix in the fake) but must not refute
    out = await m.handle_code_verify(
        CodeVerifyInput(kind="calls", subject="g", object="DB::helper")
    )
    assert out.verdict == "not_established" and out.reason == "no_edge_subject_unqualified"


def test_render_co_change_gaps() -> None:
    prs = [
        {"pr_number": 1, "files": ["a.cpp", "b.cpp", "t.sql"]},
        {"pr_number": 2, "files": ["a.cpp", "t.sql"]},
        {"pr_number": 3, "files": ["a.cpp", "z.cpp"]},
    ]
    gaps = render_co_change_gaps(prs, ["a.cpp", "b.cpp"], min_prs=2)
    assert gaps == [{"path": "t.sql", "prs": 2, "of": 3}]  # z.cpp: once only; b.cpp: present


def test_render_co_change_gaps_precision_floor() -> None:
    # 50 PRs touching a.cpp; t.sql co-changes in 8 (16%), u.cpp in 3 (6%),
    # hub.cpp in 20 (40%). Default floor: support >= 4 AND confidence >= 15%.
    prs = [
        {
            "pr_number": i,
            "files": ["a.cpp"]
            + (["t.sql"] if i < 8 else [])
            + (["u.cpp"] if i < 3 else [])
            + (["hub.cpp"] if i < 20 else []),
        }
        for i in range(50)
    ]
    gaps = render_co_change_gaps(prs, ["a.cpp"])
    assert [g["path"] for g in gaps] == ["hub.cpp", "t.sql"]
    assert gaps[0] == {"path": "hub.cpp", "prs": 20, "of": 50}
    # cap applies after the floor
    many = [
        {"pr_number": i, "files": ["a.cpp"] + [f"f{k}.cpp" for k in range(8)]} for i in range(10)
    ]
    assert len(render_co_change_gaps(many, ["a.cpp"])) == 5


def test_pr_pack_anchors_touched_symbols_at_path_line() -> None:
    """A touched symbol renders as `path:start_line` — the same stable handle
    the caller rows already carry — so a reviewer can cite it, grep it, or
    resolve it through code.enclosing without a second lookup. Rows that do
    not know their line fall back to the bare path rather than `path:0`."""
    import asyncio

    from server.code.app.index.packs import _anchor, build_pr_pack

    assert _anchor({"path": "src/a.cpp", "start_line": 42}) == "src/a.cpp:42"
    assert _anchor({"path": "src/a.cpp", "start_line": 0}) == "src/a.cpp"
    assert _anchor({"path": "src/a.cpp"}) == "src/a.cpp"
    assert _anchor({"path": "src/a.cpp", "start_line": "n/a"}) == "src/a.cpp"

    class _NoRows:
        async def query_all(self, *_a, **_k):
            return []

    repo = CodeRepo(_NoRows())  # type: ignore[arg-type]
    pack = asyncio.run(
        build_pr_pack(
            repo,
            "o",
            "ns",
            pr_number=7,
            title="t",
            commit_sha="c" * 40,
            changed_paths=["src/a.cpp"],
            changed_symbols=[
                {
                    "symbol_id": "s1",
                    "qualified_name": "DB::f",
                    "signature": "()",
                    "path": "src/a.cpp",
                    "start_line": 42,
                },
                {
                    "symbol_id": "s2",
                    "qualified_name": "DB::g",
                    "signature": "",
                    "path": "src/b.cpp",
                },
            ],
        )
    )
    assert "- `DB::f()` — src/a.cpp:42" in pack
    assert "- `DB::g` — src/b.cpp\n" in pack + "\n"
