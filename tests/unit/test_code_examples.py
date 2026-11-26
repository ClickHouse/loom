"""code.examples: call sites of a resolved symbol, each with the smallest live
chunk around the call; an unknown symbol is not found, and a symbol nobody
calls is an abstaining empty answer rather than an error."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.api.models_navigation import CodeExamplesInput
from server.code.app.enrich import (
    _example_context,
    bounded,
    indirect_test_examples,
    usage_examples,
)
from server.code.app.symbol import examples as h
from server.code.store.index import CodeRepo
from server.core.errors import NotFoundError, PreconditionFailedError
from server.settings import settings

SEED = {"symbol_id": "s-parse", "qualified_name": "DB::parseQuery", "tier": "scip"}


def _edge(path: str, line: int, caller: str = "DB::caller") -> dict[str, Any]:
    return {"path": path, "line": line, "from_qualified_name": caller, "tier": "scip"}


class _CH:
    def __init__(self, bodies: dict[tuple[str, int], str]) -> None:
        self.bodies = bodies
        self.statements: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.statements.append(" ".join(sql.split()))
        body = self.bodies.get((params["path"], params["line"]))
        return [{"content": body}] if body is not None else []


class _Repo:
    """Answers only the reads code.examples makes; anything else is an AttributeError."""

    def __init__(
        self,
        *,
        seeds: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        tested: list[str] = (),  # type: ignore[assignment]
        bodies: dict[tuple[str, int], str] | None = None,
        tests_only: bool = False,
    ) -> None:
        self.seeds, self.edges, self.tested = seeds, edges, list(tested)
        self._store = CodeRepo(_CH(bodies or {}))  # type: ignore[arg-type]
        self.edge_reads: list[list[str]] = []
        self.tests_only = tests_only

    async def innermost_chunk_at(self, org: str, ns: str, path: str, line: int) -> str | None:
        return await self._store.innermost_chunk_at(org, ns, path, line)

    async def symbols_by_ids(self, org: str, ns: str, ids: list[str]) -> list[dict[str, Any]]:
        return [s for s in self.seeds if s["symbol_id"] in ids]

    async def symbols_by_name(self, org: str, ns: str, name: str) -> list[dict[str, Any]]:
        return [s for s in self.seeds if s["qualified_name"] == name]

    async def edges_for_symbols(
        self,
        org: str,
        ns: str,
        ids: list[str],
        *,
        direction: str,
        limit: int,
        with_revision: bool,
        tests_only: bool,
    ) -> list[dict[str, Any]]:
        assert direction == "callers"
        assert not with_revision
        assert tests_only == self.tests_only
        self.edge_reads.append(list(ids))
        return self.edges

    async def test_map_for(self, org: str, ns: str, paths: list[str]) -> list[dict[str, Any]]:
        return [{"src_path": p} for p in paths if p in self.tested]


@pytest.fixture
def traces(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "code-x")
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: recorded.append(kw))
    return recorded


def _run(monkeypatch: pytest.MonkeyPatch, repo: _Repo, **kw: Any) -> Any:
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    return asyncio.run(h.handle_code_examples(CodeExamplesInput(**kw)))


def test_each_site_carries_the_chunk_around_it_and_tested_callers_come_first(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    repo = _Repo(
        seeds=[SEED],
        edges=[_edge("src/b.cpp", 40), _edge("src/a.cpp", 10), _edge("src/a.cpp", 10)],
        tested=["src/b.cpp"],
        bodies={("src/b.cpp", 40): "void b() { parseQuery(); }"},
    )
    out = _run(monkeypatch, repo, name="DB::parseQuery", top_k=3)

    sites = [(e["path"], e["line"], e["test_covered"], e["body"]) for e in out.examples]
    assert sites == [
        ("src/b.cpp", 40, True, "void b() { parseQuery(); }"),
        # a site with no live chunk still answers, with an empty body
        ("src/a.cpp", 10, False, ""),
    ]
    assert repo.edge_reads == [["s-parse"]]
    (trace,) = traces
    assert trace["op"] == "code.examples" and trace["abstain"] is False
    assert trace["returned_paths"] == ["src/b.cpp", "src/a.cpp"]
    assert trace["trace_run_id"] == out.trace_run_id != ""


def test_the_site_body_is_the_tightest_live_chunk_and_is_capped(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    repo = _Repo(
        seeds=[SEED], edges=[_edge("src/a.cpp", 10)], bodies={("src/a.cpp", 10): "x" * 9000}
    )
    out = _run(monkeypatch, repo, symbol_id="s-parse")

    assert len(out.examples[0]["body"]) == 4000
    (sql,) = repo._store._ch.statements
    assert "tombstoned_at IS NULL" in sql
    assert "ORDER BY end_line - start_line ASC LIMIT 1" in sql


def test_top_k_bounds_the_sites(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    repo = _Repo(seeds=[SEED], edges=[_edge(f"src/f{i}.cpp", i + 1) for i in range(6)])
    out = _run(monkeypatch, repo, name="DB::parseQuery", top_k=2)
    assert [e["path"] for e in out.examples] == ["src/f0.cpp", "src/f1.cpp"]


def test_indexed_test_examples_prefer_named_tests_and_distinct_callers() -> None:
    edges = [
        _edge("tests/a_support.py", 4, "tests.a_support::setup"),
        _edge("tests/test_async.py", 8, "tests.test_async::test_async_context"),
        _edge("tests/test_routing.py", 10, "tests.test_routing::test_blueprint_single"),
        _edge("tests/test_routing.py", 25, "tests.test_routing::test_blueprint_twice"),
        _edge("tests/test_routing.py", 27, "tests.test_routing::test_blueprint_twice"),
        _edge("tests/test_routing.py", 40, "tests.test_routing::test_blueprint_defaults"),
    ]
    for edge in edges:
        edge["to_qualified_name"] = "web.app::App::register_blueprint"
    repo = _Repo(seeds=[], edges=edges, tests_only=True)

    examples = asyncio.run(usage_examples(repo, "org", "code", ["s"], tests_only=True))  # type: ignore[arg-type]

    assert [site["line"] for site in examples] == [25, 10, 40]


def test_edit_examples_prefer_a_test_named_for_the_related_target() -> None:
    path = "test_routes.py"
    edges = [
        _edge(path, 5, "test_routes::test_url_for_with_scheme"),
        _edge(path, 10, "test_routes::test_blueprint_url_processors::index"),
        _edge(path, 14, "test_routes::test_blueprint_url_processors::about"),
        _edge(path, 30, "test_routes::test_url_for_with_anchor"),
    ]
    for edge in edges:
        edge["to_qualified_name"] = "web.helpers::url_for"
    repo = _Repo(seeds=[], edges=edges, tests_only=True)

    examples = asyncio.run(
        usage_examples(
            repo, "org", "code", ["s"], tests_only=True, related_names=("Blueprint.register",)
        )  # type: ignore[arg-type]
    )

    assert [site["line"] for site in examples] == [10, 5, 30]
    assert examples[0]["caller"] == "test_routes::test_blueprint_url_processors::index"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_setup", [False, True])
async def test_test_examples_keep_current_fallbacks_when_top_callers_are_stale(
    monkeypatch, stale_setup
):
    revision = "a" * 39 + "0"
    older = "b" * 40
    names = ["one", "two", "three"]
    prefix = (
        [("tests/test_a.py", line, "setup", older) for line in (4, 5, 6)] if stale_setup else []
    )
    candidates = [
        (f"tests/test_{suffix}.py", 10, f"test_other_{name}", revision)
        for suffix, name in zip(("b", "c", "d"), names, strict=True)
    ] + [(f"tests/test_z_{name}.py", 10, f"test_register_{name}", older) for name in names]

    class Repo:
        async def edges_for_symbols(self, *args, **kw):
            return [
                {
                    **_edge(path, line, caller),
                    "indexed_commit": commit + chr(0),
                    "to_qualified_name": "lib::register",
                }
                for path, line, caller, commit in prefix + candidates
            ]

        async def test_map_for(self, *args):
            return []

        async def source_chunks_for_span(self, org, ns, path, start, end, model, **kw):
            return [
                {
                    "start_line": start,
                    "end_line": end,
                    "indexed_commit": revision,
                    "content": "    register()\n" * (end - start + 1),
                }
            ]

    monkeypatch.setattr(settings, "embedding_model", "fake-ngram-3072")
    repo: Any = Repo()
    examples = await usage_examples(
        repo, "org", "code", ["register"], tests_only=True, context_lines=2
    )
    current = [site for site in examples if site.get("context", {}).get("commit") == revision]
    expected = names[:2] if stale_setup else names
    assert [site["caller"] for site in current] == [f"test_other_{name}" for name in expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("promoted", ["present", "missing", "timeout"])
async def test_indirect_examples_rank_related_tests_ahead_of_setup(monkeypatch, promoted) -> None:
    revision = "a" * 40
    bridge_path = "web/app.py"
    bridge_name = "web.app::App::register_blueprint"
    target = {
        "path": "web/blueprints.py",
        "kind": "method",
        "qualified_name": "web.blueprints::Blueprint::register",
    }
    test_path = "tests/test_routes.py"
    fallback_started = asyncio.Event()

    def incoming(path, line, caller, from_id, to_id, callee, commit=revision):
        return {
            **_edge(path, line, caller),
            "from_symbol_id": from_id,
            "to_symbol_id": to_id,
            "to_qualified_name": callee,
            "indexed_commit": commit,
            "confidence": 0.9,
        }

    class Repo:
        async def edges_for_symbols(self, org, ns, ids, **options):
            assert (org, ns) == ("org", "code")
            if ids == ["register"]:
                return [
                    incoming(
                        bridge_path, 9, bridge_name, "wrapper", "register", target["qualified_name"]
                    )
                ]
            assert ids == ["wrapper"] and options["tests_only"]
            assert options["caller_revision"] == revision and options["sites_per_caller"] == 2
            if promoted != "present":
                return [
                    incoming(
                        test_path, line, f"test_other_{line}", str(line), "wrapper", bridge_name
                    )
                    for line in (10, 40, 70)
                ] + [
                    incoming(
                        "tests/test_z.py",
                        line,
                        f"test_blueprint_{line}",
                        str(line),
                        "wrapper",
                        bridge_name,
                    )
                    for line in (11, 21, 31, 41, 51, 61)
                ]
            return [
                incoming("tests/a_support.py", 4, "setup", "setup", "wrapper", bridge_name),
                incoming("tests/test_async.py", 5, "test_async", "async", "wrapper", bridge_name),
                incoming(test_path, 10, "test_blueprint_hook", "hook", "wrapper", bridge_name),
                incoming(
                    test_path, 40, "test_blueprint_defaults", "defaults", "wrapper", bridge_name
                ),
                incoming(
                    test_path, 42, "test_blueprint_defaults", "defaults", "wrapper", bridge_name
                ),
                incoming(
                    test_path, 75, "test_blueprint_old", "old", "wrapper", bridge_name, "b" * 40
                ),
                incoming(
                    test_path,
                    90,
                    "test_blueprint_wrong_target",
                    "wrong",
                    "wrapper",
                    "other::register_blueprint",
                ),
            ]

        async def file_rows_for_paths(self, org, ns, paths):
            return {path: {"loc": 125, "indexed_commit": revision} for path in paths}

        async def source_chunks_for_span(self, org, ns, path, start, end, model, **options):
            assert options["with_revision"]
            if path == "tests/test_z.py":
                await fallback_started.wait()
                if promoted == "timeout":
                    await asyncio.Future()
                return []
            fallback_started.set()
            return [
                {
                    "path": path,
                    "start_line": start,
                    "end_line": end,
                    "indexed_commit": revision,
                    "content": "    app.register_blueprint(bp)\n" * (end - start + 1),
                }
            ]

    monkeypatch.setattr(settings, "embedding_model", "fake-ngram-3072")
    repo: Any = Repo()
    ready: list[dict[str, Any]] = []
    examples = await bounded(
        indirect_test_examples(repo, "org", "code", ["register"], target, revision, 2, ready=ready),
        ready,
        seconds=0.15,
    )

    expected = [
        ("test_blueprint_defaults", 40),
        ("test_blueprint_hook", 10),
        ("test_async", 5),
    ]
    if promoted != "present":
        expected = [("test_other_10", 10), ("test_other_40", 40), ("test_other_70", 70)]
    assert [(e["caller"], e["line"]) for e in examples] == expected
    assert all(
        e["evidence"] == "indirect_call" and e["via"]["symbol"] == bridge_name for e in examples
    )
    assert all(e["context"]["commit"] == revision for e in examples)


@pytest.mark.parametrize("source_position", [0, 1])
async def test_edit_context_keeps_optional_caller_source_when_aliases_share_definition(
    monkeypatch, source_position
):
    from server.code.api.models_symbol import CodeEditContextInput, CodeSymbolOutput
    from server.code.app.symbol import symbol
    from server.code.domain.units import CodeUnit

    names = ["value", "src/value.py:value"]
    context = {
        "commit": "a" * 40,
        "start_line": 1,
        "end_line": 2,
        "text": "def run():\n    value()",
    }

    async def read(request):
        via = {"symbol": "src.action::run", "path": "src/action.py", "line": 2}
        if request.name == names[source_position]:
            via["context"] = context
        unit = CodeUnit(
            path="src/value.py",
            symbol="src.value::value",
            body="def value():\n    return 7",
            examples=[{"path": "tests/test_action.py", "line": 3, "via": via}],
        )
        return CodeSymbolOutput(definitions=[unit])

    monkeypatch.setattr(symbol, "_single_edit_context", read)
    monkeypatch.setattr(symbol, "_symbol_trace", lambda *args, **kwargs: "")
    answer = await symbol._batched_edit_context(
        CodeEditContextInput(org="test-org", namespace="test-ns", names=names)
    )
    assert [target.definition_indices for target in answer.edit_targets] == [[0], [0]]
    (definition,) = answer.definitions
    assert definition.examples[0]["via"]["context"] == context


@pytest.mark.parametrize(
    ("revisions", "extra", "expected"),
    [
        (("head", "head", "head"), None, (8, 12)),
        (("old", "head", "head"), None, (10, 12)),
        (("head", "old", "head"), None, None),
        (("head", "head", "head"), (8, "prefix … line continues\n"), (9, 12)),
        (("head", "head", "head"), (11, "conflicting text\n"), (8, 10)),
    ],
)
def test_example_context_joins_adjacent_chunks_at_the_caller_revision(
    revisions: tuple[str, ...], extra: tuple[int, str] | None, expected: tuple[int, int] | None
) -> None:
    class Repo:
        async def source_chunks_for_span(self, *args: object, with_revision: bool) -> Any:
            assert with_revision
            rows = [
                {
                    "start_line": start,
                    "end_line": end,
                    "indexed_commit": revision,
                    "content": "".join(f"line {n}\n" for n in range(start, end + 1)),
                }
                for (start, end), revision in zip(
                    ((8, 9), (10, 10), (11, 12)), revisions, strict=True
                )
            ]
            if extra is not None:
                line, text = extra
                rows.append(
                    {
                        "start_line": line,
                        "end_line": line,
                        "indexed_commit": "head",
                        "content": text,
                    }
                )
            return rows

    site = {"path": "src/cli.py", "line": 10}
    repo: Any = Repo()
    result = asyncio.run(_example_context(repo, "o", "n", site, 2, "model", "head"))
    if expected is None:
        assert result == site
    else:
        start, end = expected
        assert result["context"] == {
            "commit": "head",
            "start_line": start,
            "end_line": end,
            "text": "".join(f"line {n}\n" for n in range(start, end + 1)),
        }


def test_a_symbol_nobody_calls_abstains_with_an_empty_answer(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    out = _run(monkeypatch, _Repo(seeds=[SEED], edges=[]), name="DB::parseQuery")
    assert out.examples == []
    (trace,) = traces
    assert trace["abstain"] is True and trace["retrieval_risk"] == "abstain"


def test_an_unknown_symbol_is_not_found_and_records_no_trace(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    from server.code.app.index import symbol_forms

    async def no_tolerant_match(*_a: Any, **_k: Any) -> tuple[list[dict[str, Any]], str]:
        return [], ""

    monkeypatch.setattr(symbol_forms, "resolve_tolerant", no_tolerant_match)
    with pytest.raises(NotFoundError, match="DB::nothing"):
        _run(monkeypatch, _Repo(seeds=[SEED], edges=[]), name="DB::nothing")
    assert traces == []


def test_the_kill_switch_refuses_before_any_read(
    monkeypatch: pytest.MonkeyPatch, traces: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", False)
    with pytest.raises(PreconditionFailedError):
        _run(monkeypatch, _Repo(seeds=[], edges=[]), name="DB::parseQuery")


@pytest.mark.parametrize("top_k", [0, 11])
def test_top_k_outside_one_to_ten_is_refused(top_k: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CodeExamplesInput(name="f", top_k=top_k)
