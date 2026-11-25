"""Stored chunk presentation text cannot become a source hit or file line."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.app import message_grep
from server.code.app.grep import _matching_lines, handle_code_grep
from server.code.app.index.ingest import ingest_file
from server.code.app.symbol.uses import _uses_lines
from server.code.domain.search_scope import build_scope
from server.code.store.index import CodeRepo
from server.core.ch import get_client
from server.core.errors import ValidationError
from server.embedding.fake import FakeNGramEmbedder
from server.settings import settings
from tests.integration._resources import unique_org


@pytest.mark.asyncio
async def test_stored_chunks_retain_kind_and_physical_source_lines(ch_client: Any) -> None:
    org, ns = unique_org("grep-source-lines"), "source-lines"
    ch_client.insert(
        "loom.code_chunks",
        [
            [
                org,
                ns,
                "m",
                "helpers.py",
                uuid.uuid4(),
                "function",
                "response",
                10,
                12,
                'Documents target.\ndef response():\n    """Use target."""\n    return target()\n',
                [0.0] * settings.embedding_dim,
                1,
            ],
            [
                org,
                ns,
                "m",
                "container.py",
                uuid.uuid4(),
                "class_skeleton",
                "Container",
                1,
                100,
                "class Container:\n    target = 1\n",
                [0.0] * settings.embedding_dim,
                1,
            ],
            [
                org,
                ns,
                "m",
                "endpoint.cpp",
                uuid.uuid4(),
                "function",
                "bind",
                20,
                22,
                'Documents binding.\nvoid bind() {\n    throw Exception("Cannot bind endpoint");\n}\n',
                [0.0] * settings.embedding_dim,
                1,
            ],
        ],
        column_names=[
            "org",
            "namespace",
            "embedding_model",
            "path",
            "chunk_id",
            "kind",
            "scope",
            "start_line",
            "end_line",
            "content",
            "embedding",
            "version",
        ],
    )
    await get_client().connect()
    repo = CodeRepo()
    chunks = await repo.grep_chunks(org, ns, "target")
    assert {row["kind"] for row in chunks} == {"function", "class_skeleton"}
    matches, capped = _matching_lines(chunks, {}, CodeGrepInput(pattern="target"), None, 20)
    expected = [("helpers.py", 11), ("helpers.py", 12)]
    assert not capped
    assert [(hit["path"], hit["line"]) for hit in matches] == expected
    assert [(hit["path"], hit["line"]) for hit in _uses_lines(chunks, ["target"], {})] == expected

    message_chunks, admitted = await repo.grep_message_chunks(org, ns, ["Cannot bind"])
    assert admitted == 1
    assert [row["kind"] for row in message_chunks] == ["function"]
    ranked = message_grep.rank_statements(
        message_grep.split_message("Cannot bind endpoint"), message_chunks
    )
    assert [(statement.path, statement.line) for statement in ranked] == [("endpoint.cpp", 21)]


@pytest.mark.asyncio
async def test_related_grep_returns_the_bound_argument_and_helper(ch_client, monkeypatch) -> None:
    org, ns = unique_org("grep-dependencies"), "source"
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_search_cursor_enabled", True)
    monkeypatch.setattr(settings, "jwt_secret", "grep-test-cursor-signing-value")
    embedder = FakeNGramEmbedder(dim=settings.embedding_dim)
    monkeypatch.setattr(settings, "embedding_model", embedder.model_name)
    await get_client().connect()
    repo = CodeRepo()
    await ingest_file(
        repo,
        org,
        ns,
        path="src/client.py",
        source=b"""def helper():
    return 7

class Client:
    @property
    def option(self):
        return helper()

def use(client: Client):
    # A short excerpt for the definition does not reach the call.
    # A related match can still be in the same function.

    return consume(client.option, extra=True)
""",
        commit_sha="a" * 40,
        embedding_model=embedder.model_name,
        embedder=embedder,
    )
    args = CodeGrepInput(
        org=org,
        namespace=ns,
        pattern="consume(client.option)",
        related_on_miss=True,
        include_dependencies=True,
        include_body=False,
    )
    out = await handle_code_grep(args)
    assert not out.matches and out.related and out.related.matches
    assert [d.qualified_name for d in out.related.dependencies] == [
        "src.client::Client::option",
        "src.client::helper",
    ]
    assert [d.body.splitlines()[-1].strip() for d in out.related.dependencies] == [
        "return helper()",
        "return 7",
    ]
    malformed = args.model_copy(
        update={"pattern": "consume(other.option)|register(handler|apply()"}
    )
    member = await handle_code_grep(malformed)
    assert not member.matches and member.related
    assert member.related.pattern == ".option"
    assert member.related.matches == out.related.matches
    assert not member.related.dependencies
    assert "across receivers" in member.note
    disabled = await handle_code_grep(malformed.model_copy(update={"related_on_miss": False}))
    assert not disabled.matches and disabled.related is None
    for competing in (
        "consume(other.option)|register(worker.queue",
        "consume(other.option, worker.queue)|register(",
        "consume(other.option)|register(handler, worker.queue",
        "consume(other.option)|worker.queue|register(",
    ):
        ambiguous_member = await handle_code_grep(
            malformed.model_copy(update={"pattern": competing})
        )
        assert not ambiguous_member.matches and ambiguous_member.related is None
    locations = await handle_code_grep(args.model_copy(update={"context_lines": 0}))
    assert locations.related and locations.related.matches == out.related.matches
    assert not locations.related.dependencies
    batch = args.model_copy(update={"pattern": ["consume(client.option)", "client.option"]})
    exact_batch = await handle_code_grep(batch.model_copy(update={"include_dependencies": False}))
    enriched_batch = await handle_code_grep(batch)
    assert enriched_batch.matches == exact_batch.matches == out.related.matches
    assert enriched_batch.related and not enriched_batch.related.matches
    assert enriched_batch.related.dependencies == out.related.dependencies
    ambiguous = await handle_code_grep(
        batch.model_copy(update={"pattern": [*batch.pattern, "consume(other.option)"]})
    )
    assert ambiguous.matches == exact_batch.matches and ambiguous.related is None
    await ingest_file(
        repo,
        org,
        ns,
        path="tests/test_client.py",
        source=b"def test_option():\n    assert True\n",
        commit_sha="a" * 40,
        embedding_model=embedder.model_name,
        embedder=embedder,
    )
    mixed = args.model_copy(
        update={"pattern": ["consume(client.option)", "def test_option"], "include_body": True}
    )
    exact = await handle_code_grep(mixed.model_copy(update={"related_on_miss": False}))
    enriched = await handle_code_grep(mixed)
    assert enriched.matches == exact.matches
    assert [hit["path"] for hit in enriched.matches] == ["tests/test_client.py"]
    assert enriched.definitions == exact.definitions
    assert enriched.related and enriched.related.matches == out.related.matches
    assert "consume(client.option, extra=True)" in enriched.related.definitions[0]["body"]
    assert enriched.related.dependencies == out.related.dependencies
    scoped = await handle_code_grep(mixed.model_copy(update={"path_prefix": "tests/"}))
    assert scoped.matches == exact.matches and scoped.related is None
    same_scope = args.model_copy(
        update={"pattern": ["consume(client.option)", "def use(client:"], "context_lines": 2}
    )
    excerpts = await handle_code_grep(same_scope)
    assert not any("client.option" in row["text"] for row in excerpts.contexts)
    assert excerpts.related and excerpts.related.matches == out.related.matches
    assert excerpts.related.dependencies == out.related.dependencies
    full = await handle_code_grep(same_scope.model_copy(update={"include_body": True}))
    assert full.related and full.related.matches == out.related.matches
    assert "consume(client.option, extra=True)" in full.definitions[0]["body"]
    assert not full.related.definitions
    wide = args.model_copy(
        update={"pattern": ["consume(client.option", "def "], "context_lines": 3, "limit": 1}
    )
    first = await handle_code_grep(wide)
    assert len(first.matches) == 1 and first.next_cursor and first.truncated
    assert first.related is None
    focused = await handle_code_grep(wide.model_copy(update={"related_on_prefix": True}))
    assert focused.matches == first.matches and focused.next_cursor and focused.truncated
    assert focused.related and focused.related.matches == out.related.matches
    assert focused.related.dependencies == out.related.dependencies
    next_exact = await handle_code_grep(wide.model_copy(update={"cursor": first.next_cursor}))
    next_focused = await handle_code_grep(
        wide.model_copy(update={"cursor": focused.next_cursor, "related_on_prefix": True})
    )
    assert next_focused.matches == next_exact.matches and next_focused.related is None


@pytest.mark.asyncio
async def test_named_forwarder_returns_its_verified_implementation(ch_client, monkeypatch) -> None:
    org, ns = unique_org("grep-forwarding"), "source"
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    embedder = FakeNGramEmbedder(dim=settings.embedding_dim)
    monkeypatch.setattr(settings, "embedding_model", embedder.model_name)
    await get_client().connect()
    await ingest_file(
        CodeRepo(),
        org,
        ns,
        path="src/service.py",
        source=b'''def implement(value):
    return value + 1

def dispatch(value):
    """Delegate implementation to the internal handler."""
    return implement(value)

class Queue:
    def dispatch(self, value):
        return self.pending.append(value)

def alternate(value):
    return value - 1

def other_dispatch(value):
    return alternate(value)

def conditional(value):
    if value:
        return implement(value)
    return 0

class Service:
    def implementation(self, value):
        return value * 2

    def forward(self, value):
        return self.implementation(value)
''',
        commit_sha="a" * 40,
        embedding_model=embedder.model_name,
        embedder=embedder,
    )
    args = CodeGrepInput(
        org=org,
        namespace=ns,
        pattern=["def dispatch", "def conditional"],
        include_dependencies=True,
    )
    out = await handle_code_grep(args)
    exact = await handle_code_grep(args.model_copy(update={"include_dependencies": False}))
    assert out.matches == exact.matches
    assert out.definitions == exact.definitions
    assert out.related and not out.related.matches and exact.related is None
    assert out.related.pattern == "src.service::dispatch"
    [dependency] = out.related.dependencies
    assert dependency.qualified_name == "src.service::implement"
    assert dependency.body.splitlines() == ["def implement(value):", "    return value + 1"]
    for changes in (
        {"include_body": False},
        {"context_lines": 0},
        {"pattern": "dispatch"},
        {"pattern": "def conditional"},
        {"pattern": ["def dispatch", "def other_dispatch"]},
        {"pattern": ["def dispatch", "def implement"]},
    ):
        assert (await handle_code_grep(args.model_copy(update=changes))).related is None
    enclosed = await handle_code_grep(
        args.model_copy(update={"pattern": ["class Service", "def forward"]})
    )
    assert any("return value * 2" in d.get("body", "") for d in enclosed.definitions)
    assert enclosed.related is None


@pytest.mark.asyncio
async def test_ending_proof_survives_recovery_of_nested_class_source(
    ch_client, monkeypatch
) -> None:
    org, ns = unique_org("grep-recovered-ending"), "source"
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    embedder = FakeNGramEmbedder(dim=settings.embedding_dim)
    monkeypatch.setattr(settings, "embedding_model", embedder.model_name)
    await get_client().connect()
    repo = CodeRepo()
    path = "nested.py"
    pattern = "def exact():\n"
    await ingest_file(
        repo,
        org,
        ns,
        path=path,
        source=b"class Outer:\n    class Middle:\n        class Inner:\n            def exact():\n                return 1\n",
        commit_sha="b" * 40,
        embedding_model=embedder.model_name,
        embedder=embedder,
    )
    pool = await repo.grep_chunks(org, ns, pattern, limit=3)
    assert [row["kind"] for row in pool] == ["class_skeleton"] * 3
    result = await handle_code_grep(CodeGrepInput(org=org, namespace=ns, pattern=pattern, limit=1))
    assert [(row["path"], row["line"], row["text"]) for row in result.matches] == [
        (path, 4, "def exact():"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("early_hits", [4, 20])
@pytest.mark.parametrize("tail_pattern", ["def tail_requested", "def tail_requested():\n"])
async def test_coding_grep_keeps_distinct_batch_patterns_on_first_page(
    ch_client: Any, monkeypatch: pytest.MonkeyPatch, early_hits: int, tail_pattern: str
) -> None:
    from server.transport.mcp_defaults import mcp_default_arguments

    org, ns = unique_org("grep-batch-coverage"), "source"
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_search_cursor_enabled", True)
    monkeypatch.setattr(settings, "jwt_secret", "grep-test-batch-cursor-signing-value")
    embedder = FakeNGramEmbedder(dim=settings.embedding_dim)
    monkeypatch.setattr(settings, "embedding_model", embedder.model_name)
    await get_client().connect()
    repo = CodeRepo()
    for path, source in (
        (
            "src/early.py",
            "".join(f"def repeat_{index}():\n    return 1\n\n" for index in range(early_hits)),
        ),
        ("src/late.py", "def requested():  # match_alpha match_beta\n    return 2\n"),
        ("src/tail.py", "def tail_requested():\n    return 4\n"),
        ("outside.py", "def forbidden():\n    return 3\n"),
    ):
        await ingest_file(
            repo,
            org,
            ns,
            path=path,
            source=source.encode(),
            commit_sha="b" * 40,
            embedding_model=embedder.model_name,
            embedder=embedder,
        )
    ch_client.insert(
        "loom.code_chunks",
        [
            [
                org,
                ns,
                embedder.model_name,
                path,
                uuid.uuid4(),
                "function",
                scope,
                1,
                2 if path.startswith("projected/") else 1,
                content,
                [0.0] * settings.embedding_dim,
                1,
                "b" * 40,
            ]
            for path, scope, content in (
                ("src/early.py", "repeat_0", "# def requested\ndef repeat_0():\n"),
                ("edges/a.py", "unicode_marker", "needle … line continues\u00a0\n"),
                ("edges/b.py", "real_needle", "real needle source\n"),
                ("edges/c.py", "trailing_cr", "target\r\n"),
                ("edges/d.py", "interior_cr", "real target\r rest\n"),
                ("endings/0.py", "synthetic_end", "check_ending()\n"),
                ("endings/a.py", "unterminated", "check_ending()"),
                ("endings/b.py", "line_feed", "check_ending()\n"),
                ("endings/c.py", "carriage_return", "check_ending()\r\n"),
                ("endings/d.py", "older_chunk", "check_ending()\n"),
                ("projected/a.reference", "", "check_ending()\n… clipped reference\n"),
                ("projected/b.ipynb", "cell", "def cell():\n    check_ending()\n"),
                ("projected/c.ipynb", "cell", "def cell():\n    check_ending()\n"),
                ("projected/z.py", "physical", "def physical():\n    check_ending()\n"),
            )
        ],
        column_names=[
            "org",
            "namespace",
            "embedding_model",
            "path",
            "chunk_id",
            "kind",
            "scope",
            "start_line",
            "end_line",
            "content",
            "embedding",
            "version",
            "indexed_commit",
        ],
    )
    ch_client.insert(
        "loom.code_files",
        [
            [org, ns, "endings/0.py", "python", 1, "b" * 40],
            [org, ns, "endings/a.py", "python", 1, "b" * 40],
            [org, ns, "endings/b.py", "python", 2, "b" * 40],
            [org, ns, "endings/c.py", "python", 2, "b" * 40],
            [org, ns, "endings/d.py", "python", 2, "c" * 40],
            [org, ns, "projected/a.reference", "reference", 3, "b" * 40],
            [org, ns, "projected/b.ipynb", "python", 5, "b" * 40],
            [org, ns, "projected/c.ipynb", "python", 5, "b" * 40],
            [org, ns, "projected/z.py", "python", 3, "b" * 40],
        ],
        column_names=["org", "namespace", "path", "lang", "loc", "indexed_commit"],
    )
    args = {
        "org": org,
        "namespace": ns,
        "pattern": [
            "def ",
            "def REQUESTED",
            "match_alpha",
            "match_beta",
            tail_pattern,
            "def forbidden",
        ],
        "path_globs": ["src/**"],
        "case_sensitive": False,
        "limit": 3,
    }
    path_order = await handle_code_grep(CodeGrepInput(**args))
    assert [row["path"] for row in path_order.matches] == ["src/early.py"] * 3

    request = CodeGrepInput(**mcp_default_arguments("code.grep", args, toolset="coding"))
    page = await handle_code_grep(request)
    assert len(page.matches) == 3
    assert [row["path"] for row in page.matches] == ["src/early.py", "src/late.py", "src/tail.py"]
    assert any("def requested():" in row.get("body", "") for row in page.definitions)

    assert page.next_cursor
    next_page = await handle_code_grep(request.model_copy(update={"cursor": page.next_cursor}))
    first_hits = {(row["path"], row["line"]) for row in page.matches}
    assert next_page.matches
    assert not first_hits.intersection((row["path"], row["line"]) for row in next_page.matches)

    if early_hits == 20:
        witnesses = await repo.grep_pattern_chunks(
            org,
            ns,
            ["def REQUESTED", "match_alpha", "match_beta", tail_pattern, "def forbidden"],
            case_sensitive=False,
            path_prefix="",
            scope=build_scope(path_globs=["src/**"]),
            exclude_paths=[],
            limit=3,
        )
        assert [row["path"] for row in witnesses] == ["src/late.py", "src/tail.py"]
        exact_lines = await repo.grep_pattern_chunks(
            org,
            ns,
            ["needle", "target\r"],
            case_sensitive=True,
            path_prefix="edges/",
            scope=build_scope(),
            exclude_paths=[],
            limit=2,
        )
        assert [row["path"] for row in exact_lines] == ["edges/b.py", "edges/d.py"]
        endings = ["check_ending()\n", "check_ending()\r\n"]
        ending_witnesses = await repo.grep_pattern_chunks(
            org,
            ns,
            endings,
            case_sensitive=True,
            path_prefix="endings/",
            scope=build_scope(),
            exclude_paths=[],
            limit=3,
        )
        ending_hits = await handle_code_grep(
            CodeGrepInput(org=org, namespace=ns, pattern=endings, path_prefix="endings/")
        )
        assert [row["path"] for row in ending_witnesses] == ["endings/b.py", "endings/c.py"]
        assert [(row["path"], row["line"], row["text"]) for row in ending_hits.matches] == [
            ("endings/b.py", 1, "check_ending()"),
            ("endings/c.py", 1, "check_ending()"),
        ]
        scalar_ending = await handle_code_grep(
            CodeGrepInput(org=org, namespace=ns, pattern=endings[0], path_prefix="endings/")
        )
        assert [row["path"] for row in scalar_ending.matches] == ["endings/b.py"]
        projected = CodeGrepInput(
            org=org,
            namespace=ns,
            pattern=endings,
            path_prefix="projected/",
            exclude_globs=["projected/z.py"],
        )
        assert not (await handle_code_grep(projected)).matches
        plain = await handle_code_grep(projected.model_copy(update={"pattern": "check_ending()"}))
        assert [row["path"] for row in plain.matches] == [
            "projected/a.reference",
            "projected/b.ipynb",
            "projected/c.ipynb",
        ]
        assert not await repo.grep_pattern_chunks(
            org,
            ns,
            endings,
            case_sensitive=True,
            path_prefix="projected/",
            scope=build_scope(exclude_globs=["projected/z.py"]),
            exclude_paths=[],
            limit=3,
        )
        physical = await handle_code_grep(
            CodeGrepInput(
                org=org, namespace=ns, pattern=endings[0], path_prefix="projected/", limit=1
            )
        )
        assert [row["path"] for row in physical.matches] == ["projected/z.py"]

        async def missing_pool(*args: Any, **kwargs: Any) -> None:
            return None

        with monkeypatch.context() as patch:
            patch.setattr("server.code.app.grep._read_pool_settling", missing_pool)
            cursor_request = request.model_copy(update={"cursor": page.next_cursor})
            with pytest.raises(ValidationError, match="start a new search"):
                await handle_code_grep(cursor_request)

        async def timed_out(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise TimeoutError

        with monkeypatch.context() as patch:
            patch.setattr(
                "server.code.app.grep._missing_chunk_patterns",
                lambda *a, **kw: pytest.fail(
                    "the supplemental pre-scan requires more than the minimum deadline"
                ),
            )
            short = await handle_code_grep(request.model_copy(update={"deadline_ms": 200}))
            assert short.matches == path_order.matches
            assert "batch pattern scan skipped" in short.partial_reason

        monkeypatch.setattr(CodeRepo, "grep_pattern_chunks", timed_out)
        partial = await handle_code_grep(request)
        assert partial.matches == path_order.matches
        assert partial.partial
        assert "batch pattern scan" in partial.partial_reason
