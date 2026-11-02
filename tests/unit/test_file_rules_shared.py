"""An explicit rule attaches to its file, and a repository's
team namespace carries shared file rules.

* memory.set stores `files` — the caller's, else detected from the value,
  else the existing row's;
* memory.for_file reads `extra_namespaces` (the team namespace) beside the
  caller's, skipping an unreadable one, constraint-first, team rows marked;
* a code repo entry names its `team_namespace` and code.repos exposes it.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.api.models_crud import MemorySetInput
from server.memory.api.models_for_file import MemoryForFileInput
from server.memory.app import for_file as mff
from server.memory.domain import files as mf
from tests.unit.code_fixtures import repo_config

# --- memory.set: files -------------------------------------------------------


def test_explicit_files_are_normalised_deduped_and_capped() -> None:
    got = mf.files_for_set(
        ["./src/a.cpp", "src\\b.h", "SRC/a.cpp", "", "  c.py  "], "text naming x.py", "", None
    )
    assert got == ["src/a.cpp", "src/b.h", "c.py"]
    many = [f"src/f{i}.cpp" for i in range(50)]
    assert len(mf.files_for_set(many, "", "", None)) == mf.MAX_FILES
    # [] is an explicit clear, even over detectable text and an existing row
    assert mf.files_for_set([], "src/a.cpp", "", SimpleNamespace(files=["old.cpp"])) == []


def test_input_model_bounds_files() -> None:
    base = {"org": "o", "namespace": "n", "agent": "a", "memory_key": "k", "value": "v"}
    assert MemorySetInput(**base).files is None
    assert MemorySetInput(**base, files=["a.py"]).files == ["a.py"]
    with pytest.raises(ValueError):
        MemorySetInput(**base, files=[f"f{i}.py" for i in range(33)])
    with pytest.raises(ValueError):
        MemorySetInput(**base, files=["x" * 1025])


def test_paths_detected_from_the_value() -> None:
    rule = (
        "Don't change the default bucket count in src/Interpreters/GraceHashJoin.cpp "
        "without the join benchmark; see also HashJoin.h."
    )
    assert mf.detect_value_files(rule) == ["src/Interpreters/GraceHashJoin.cpp", "HashJoin.h"]
    # a bare filename alone
    assert mf.detect_value_files("GraceHashJoin.cpp owns the buckets") == ["GraceHashJoin.cpp"]
    # dedupe, ./ and a line suffix dropped
    assert mf.detect_value_files("./src/a.py:12 and src/a.py and SRC/A.PY") == ["src/a.py"]


def test_no_false_positive_on_urls_versions_and_library_names() -> None:
    assert mf.detect_value_files("see https://github.com/o/r/blob/master/src/z.py") == []
    assert mf.detect_value_files("www.example.com/docs/index.html is the page") == []
    assert mf.detect_value_files("upgrade to 26.8.1.187, then v1.2.3 and 3.12") == []
    assert mf.detect_value_files("we run node.js 20") == []
    assert mf.detect_value_files("the team prefers short meetings") == []


def test_detection_is_capped() -> None:
    text = " ".join(f"src/m{i}.cpp" for i in range(40))
    got = mf.detect_value_files(text)
    assert 0 < len(got) <= mf.MAX_FILES and len(set(got)) == len(got)


def test_a_rewrite_that_names_no_file_keeps_the_rows() -> None:
    existing = SimpleNamespace(files=["src/keep.cpp"])
    assert mf.files_for_set(None, "no path here", "", existing) == ["src/keep.cpp"]
    # new paths in the new value replace them
    assert mf.files_for_set(None, "now src/new.cpp", "", existing) == ["src/new.cpp"]
    # a dict value is read through its serialization
    ser = json.dumps({"rule": "edit gen/x.py via the template"})
    assert mf.files_for_set(None, {"rule": "…"}, ser, None) == ["gen/x.py"]


_NOW = datetime(2026, 9, 25, 9, 0)


def _mrow(**over: Any) -> memory_repo.MemoryRow:
    from server.core.hashing import sha256_value

    base: dict[str, Any] = {
        "memory_id": "51c3cdf5-7ea2-4b79-9d34-a6b5d7a9a2b2",
        "org": "test",
        "namespace": "personal",
        "agent": "claude-code",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "rules/join/bucket_count",
        "memory_type": "semantic",
        "value": "old rule",
        "confidence": 1.0,
        "version": 100,
        "pinned": False,
        "created_at": _NOW,
        "expires_at": None,
        "entities": [],
        "content_sha256": sha256_value("old rule"),
        "files": ["src/Interpreters/GraceHashJoin.cpp"],
    }
    base.update(over)
    return memory_repo.MemoryRow(**base)


def test_version_row_builders_carry_files() -> None:
    existing = _mrow()
    assert memory_repo.build_memory_version_row(existing, version=2)["files"] == [
        "src/Interpreters/GraceHashJoin.cpp"
    ]
    assert memory_repo.build_memory_version_row(existing, files=["b.py"])["files"] == ["b.py"]
    assert memory_repo.build_embedding_row(files=["src/a.cpp"])["files"] == ["src/a.cpp"]
    assert memory_repo.build_embedding_row()["files"] == []


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None

    def rows(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rs in self.inserts if t == table for r in rs]


def _wire(monkeypatch: pytest.MonkeyPatch, existing: memory_repo.MemoryRow | None) -> _CH:
    """memory.set against a fake store."""
    import server.memory.app.blacklist_guard as _bl
    import server.memory.app.conflicts as _cf
    from server.memory.app import handlers as mem_handlers
    from server.memory.app import restore as _restore_mod
    from server.memory.app import stack as _stack_mod

    ch = _CH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch)

    async def _existing(*_: Any, **__: Any) -> memory_repo.MemoryRow | None:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _existing)
    monkeypatch.setattr("server.memory.store.get_by_id", _existing)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(_bl, "get_client", lambda: ch, raising=False)
    monkeypatch.setattr(_cf, "get_client", lambda: ch, raising=False)
    return ch


def _set(value: Any, **kw: Any) -> MemorySetInput:
    return MemorySetInput(
        org="test",
        namespace="personal",
        agent="claude-code",
        memory_key="rules/join/bucket_count",
        value=value,
        classify=False,
        **kw,
    )


@pytest.mark.asyncio
async def test_memory_set_stores_explicit_files_on_both_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import write as write_mod

    ch = _wire(monkeypatch, None)
    await write_mod.handle_memory_set(
        _set("Run the join benchmark first.", files=["./src/Interpreters/GraceHashJoin.cpp"])
    )
    assert ch.rows("loom.memory")[0]["files"] == ["src/Interpreters/GraceHashJoin.cpp"]
    assert ch.rows("loom.memory_embeddings")[0]["files"] == ["src/Interpreters/GraceHashJoin.cpp"]


@pytest.mark.asyncio
async def test_memory_set_detects_files_from_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import write as write_mod

    ch = _wire(monkeypatch, None)
    await write_mod.handle_memory_set(
        _set(
            "Don't change the default bucket count in GraceHashJoin.cpp without the join benchmark."
        )
    )
    assert ch.rows("loom.memory")[0]["files"] == ["GraceHashJoin.cpp"]
    assert ch.rows("loom.memory_embeddings")[0]["files"] == ["GraceHashJoin.cpp"]


@pytest.mark.asyncio
async def test_an_update_keeps_the_old_files(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import write as write_mod

    ch = _wire(monkeypatch, _mrow())
    await write_mod.handle_memory_set(_set("Run the join benchmark before any change."))
    assert ch.rows("loom.memory")[0]["files"] == ["src/Interpreters/GraceHashJoin.cpp"]


# --- memory.for_file: extra_namespaces ----------------------------------------

_NOWFF = datetime(2026, 9, 25, 12, 0)


class _NsCH:
    """Answers each namespace's query with its own rows; records the calls."""

    def __init__(self, by_ns: dict[str, list[dict[str, Any]]], fail: set[str] | None = None):
        self.by_ns = by_ns
        self.fail = fail or set()
        self.calls: list[dict[str, Any]] = []
        self.sql = ""

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        p = dict(params or {})
        self.sql = sql
        self.calls.append(p)
        if p["ns"] in self.fail:
            raise RuntimeError("boom")
        return [dict(r) for r in self.by_ns.get(p["ns"], [])]


def _row(key: str, value: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "memory_key": key,
        "memory_type": "semantic",
        "value": value,
        "tags": [],
        "confidence": 0.9,
        "pinned": 0,
        "status_text": "active",
        "agent": "claude-code",
        "updated_at": datetime(2026, 9, 25, 9, 0),
        "matched_files": ["src/a.cpp"],
        "actor": "human:alice",
        "memory_id": f"id-{key}",
        "conflict_with": [],
    }
    base.update(over)
    return base


def _allow(readable: set[str]):
    async def fake(org: str, ns: str, role: Any = None) -> bool:
        return ns in readable

    return fake


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    real = mff.render_block

    def _render(*a: Any, **kw: Any):
        kw.setdefault("now", _NOWFF)
        return real(*a, **kw)

    monkeypatch.setattr(mff, "render_block", _render)


@pytest.mark.asyncio
async def test_extra_namespace_rows_merge_constraint_first_with_author_and_age(
    monkeypatch: pytest.MonkeyPatch,
    fixed_now: None,
) -> None:
    from server.auth import authz

    ch = _NsCH(
        {
            "jdoe": [
                _row("my_note", "I prefer small diffs here.", actor="human:jdoe"),
                _row("shared", "same key, mine", actor="human:jdoe"),
            ],
            "code-clickhouse": [
                _row(
                    "rules/join/bucket_count",
                    "Don't change the default bucket count without the join benchmark.",
                    tags=["constraint"],
                    confidence=0.5,
                    updated_at=datetime(2026, 9, 22, 11, 0),
                ),
                _row("shared", "same key, team", actor="process:sa:bob"),
            ],
        }
    )
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    monkeypatch.setattr(authz, "row_in_scope", _allow({"code-clickhouse"}))
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(
            org="clickhouse",
            namespace="jdoe",
            path="src/a.cpp",
            tags=["repo:x"],
            extra_namespaces=["code-clickhouse", "jdoe"],
        )
    )
    # one query per namespace; the tag filter rides the caller's only
    assert [c["ns"] for c in ch.calls] == ["jdoe", "code-clickhouse"]
    assert ch.calls[0]["tags"] == ["repo:x"] and ch.calls[1]["tags"] == []
    assert "actor" in ch.sql and "conflict_with" in ch.sql
    keys = [(h.namespace, h.memory_key) for h in out.hits]
    assert keys[0] == ("code-clickhouse", "rules/join/bucket_count")  # constraint first
    # dedupe is by (namespace, key): the same key in two namespaces is two rows
    assert ("jdoe", "shared") in keys and ("code-clickhouse", "shared") in keys
    assert out.total == 4
    assert out.hits[0].actor == "human:alice" and out.hits[0].namespace == "code-clickhouse"
    lines = out.rendered.splitlines()
    assert lines[1] == (
        "- Team rule (by alice, 3 days ago) [constraint]: Don't change the default bucket "
        "count without the join benchmark. [id: rules/join/bucket_count]"
    )
    assert "- Team rule (by sa:bob, 3 hours ago): same key, team [id: shared]" in lines
    # a personal rule renders as before: no label, no author
    assert "- my_note: I prefer small diffs here." in lines
    assert "- shared: same key, mine" in lines
    # the removal footer, once, naming the code namespace
    assert lines[-1].startswith(
        '- A team rule wrong or outdated? memory.delete(namespace="code-clickhouse", memory_key=<id>)'
    )
    assert sum("memory.delete" in ln for ln in lines) == 1


@pytest.mark.asyncio
async def test_no_team_rules_no_footer(monkeypatch: pytest.MonkeyPatch, fixed_now: None) -> None:
    ch = _NsCH({"jdoe": [_row("mine", "v")]})
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(org="o", namespace="jdoe", path="src/a.cpp")
    )
    assert out.rendered == "Loom: 1 memory anchored to src/a.cpp — read before editing:\n- mine: v"


@pytest.mark.asyncio
async def test_a_newer_conflicting_rule_is_shown_above_the_older_one(
    monkeypatch: pytest.MonkeyPatch,
    fixed_now: None,
) -> None:
    """Reuses the conflict check's cross-link (`conflict_with`): the newer
    of two linked rules goes directly above the older, labelled."""
    from server.auth import authz

    old = _row(
        "rules/join/buckets_v1",
        "Keep 32 buckets.",
        tags=["constraint"],
        confidence=1.0,
        updated_at=datetime(2026, 9, 20, 12, 0),
        memory_id="u-old",
        status_text="conflicted",
    )
    other = _row(
        "rules/join/spill",
        "Spill path is tested by 02xxx.",
        tags=["constraint"],
        confidence=0.95,
        updated_at=datetime(2026, 9, 21, 12, 0),
        actor="human:carol",
    )
    new = _row(
        "rules/join/buckets_v2",
        "Keep 64 buckets since the 26.8 benchmark.",
        tags=["constraint"],
        confidence=0.6,
        updated_at=datetime(2026, 9, 25, 10, 0),
        actor="human:bob",
        memory_id="u-new",
        conflict_with=["u-old"],
        status_text="conflicted",
    )
    ch = _NsCH({"me": [], "code-clickhouse": [old, other, new]})
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    monkeypatch.setattr(authz, "row_in_scope", _allow({"code-clickhouse"}))
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(
            org="o",
            namespace="me",
            path="src/a.cpp",
            extra_namespaces=["code-clickhouse"],
        )
    )
    assert [h.memory_key for h in out.hits] == [
        "rules/join/buckets_v2",
        "rules/join/buckets_v1",
        "rules/join/spill",
    ]
    assert out.hits[0].supersedes_key == "rules/join/buckets_v1"
    assert out.hits[0].supersedes == "alice"
    assert out.hits[0].supersedes_kind == "rule"
    assert out.rendered.splitlines()[1].startswith(
        "- Team rule (by bob, 2 hours ago; supersedes rule by alice) [constraint]: Keep 64"
    )


@pytest.mark.asyncio
async def test_a_rule_contradicting_an_older_tagged_decision_names_it_a_decision(
    monkeypatch: pytest.MonkeyPatch,
    fixed_now: None,
) -> None:
    from server.auth import authz

    # The conflict check links rows of one type, so the older is a rule tagged `decision`.
    old = _row(
        "design/join/buckets",
        json.dumps("Decided 32 buckets."),
        tags=["decision"],
        updated_at=datetime(2026, 9, 20, 12, 0),
        memory_id="u-old",
    )
    new = _row(
        "rules/join/buckets",
        json.dumps("Keep 64 buckets."),
        tags=["constraint"],
        updated_at=datetime(2026, 9, 25, 10, 0),
        actor="human:bob",
        memory_id="u-new",
        conflict_with=["u-old"],
    )
    monkeypatch.setattr(mff, "get_client", lambda: _NsCH({"me": [], "code-clickhouse": [old, new]}))
    monkeypatch.setattr(authz, "row_in_scope", _allow({"code-clickhouse"}))
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(
            org="o", namespace="me", path="src/a.cpp", extra_namespaces=["code-clickhouse"]
        )
    )
    assert [h.memory_key for h in out.hits] == ["rules/join/buckets", "design/join/buckets"]
    assert out.hits[0].supersedes_kind == "decision" and out.hits[1].supersedes_kind == ""
    assert out.rendered.splitlines()[1].startswith(
        "- Team rule (by bob, 2 hours ago; supersedes decision by alice) [constraint]: Keep 64"
    )


@pytest.mark.asyncio
async def test_a_superseded_team_row_leaves_the_block_with_its_superseder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import authz

    old = _row("rules/old", json.dumps("Keep 32."), updated_at=datetime(2026, 9, 20, 12, 0))
    new = _row(
        "rules/new",
        json.dumps("n" * 300),
        memory_id="u-new",
        conflict_with=["id-rules/old"],
        actor="human:bob",
    )
    monkeypatch.setattr(mff, "get_client", lambda: _NsCH({"me": [], "code-clickhouse": [old, new]}))
    monkeypatch.setattr(authz, "row_in_scope", _allow({"code-clickhouse"}))
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(
            org="o",
            namespace="me",
            path="src/a.cpp",
            token_budget=100,
            extra_namespaces=["code-clickhouse"],
        )
    )
    assert [h.memory_key for h in out.hits] == ["rules/new", "rules/old"]
    assert out.truncated is True and out.tokens_estimate <= 100
    assert "rules/new" not in out.rendered and "rules/old" not in out.rendered
    assert '- … 2 more; memory.for_file(path="src/a.cpp") lists them.' in out.rendered


@pytest.mark.asyncio
async def test_an_unreadable_or_failing_extra_namespace_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import authz

    ch = _NsCH(
        {"me": [_row("mine", "v")], "secret": [_row("leak", "x")], "broken": [_row("b", "x")]},
        fail={"broken"},
    )
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    monkeypatch.setattr(authz, "row_in_scope", _allow({"broken"}))
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(
            org="o",
            namespace="me",
            path="src/a.cpp",
            extra_namespaces=["secret", "broken"],
        )
    )
    assert "secret" not in [c["ns"] for c in ch.calls]  # never queried
    assert [h.memory_key for h in out.hits] == ["mine"]
    assert "Team rule" not in out.rendered


@pytest.mark.asyncio
async def test_merged_rows_keep_top_k_and_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import authz

    ch = _NsCH(
        {
            "me": [_row(f"m{i}", "x" * 200) for i in range(3)],
            "team": [_row(f"t{i}", "y" * 200, pinned=1) for i in range(3)],
        }
    )
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    monkeypatch.setattr(authz, "row_in_scope", _allow({"team"}))
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(
            org="o",
            namespace="me",
            path="a.cpp",
            top_k=4,
            token_budget=150,
            extra_namespaces=["team"],
        )
    )
    assert out.total == 4
    assert [h.memory_key for h in out.hits][:3] == ["t0", "t1", "t2"]  # pinned before unpinned
    assert out.truncated is True and out.tokens_estimate <= 150
    assert "memory.delete" in out.rendered.splitlines()[-1]  # the footer survives the cut


def test_extra_namespaces_is_bounded() -> None:
    with pytest.raises(ValueError):
        MemoryForFileInput(org="o", namespace="n", path="a", extra_namespaces=["a", "b", "c"])


def test_render_block_without_home_marks_nothing() -> None:
    text, _, _ = mff.render_block(
        "x.py", [{"memory_key": "k", "value": "v", "tags": [], "namespace": "team"}], 300
    )
    assert "Team rule" not in text and "memory.delete" not in text


def test_age_rendering() -> None:
    now = datetime(2026, 9, 25, 12, 0)
    assert mff._age(datetime(2026, 9, 25, 11, 59, 30), now) == "just now"
    assert mff._age(datetime(2026, 9, 25, 11, 0), now) == "1 hour ago"
    assert mff._age(datetime(2026, 9, 24, 12, 0), now) == "1 day ago"
    assert mff._age(datetime(2026, 9, 25, 11, 55), now) == "5 minutes ago"
    assert mff._age(None, now) == ""


# --- code.repos: primary_namespace --------------------------------------------


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch):
    """code.repos over a fake plane."""
    import server.auth.authz as authz
    import server.code.domain.index.repos as repos
    import server.identity.app.namespace_classification as nc
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.code.app import index_status
    from server.code.app import repos as repos_mod
    from server.operations.base import Role

    def _cfg(ns: str, repo: str, branch: str) -> Any:
        return repo_config(
            org="clickhouse",
            namespace=ns,
            repo=repo,
            git_branch=branch,
            classification="standard",
            org_readable=True,
        )

    entries = [
        _cfg("code-clickhouse-26-8", "ClickHouse/ClickHouse", "26.8"),
        _cfg("code-clickhouse", "ClickHouse/ClickHouse", "master"),
        _cfg("code-example-private", "ClickHouse/example-private", "master"),
        _cfg("code-loom", "ClickHouse/loom", "main"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    monkeypatch.setattr(index_status, "require_code_intel", lambda: None)

    async def _refresh() -> None:
        return None

    monkeypatch.setattr("server.code.app.index.repo_registry.refresh_db_repos", _refresh)

    async def _status(cfg: Any) -> Any:
        return SimpleNamespace(
            org=cfg.org,
            namespace=cfg.namespace,
            repo=cfg.repo,
            branch=cfg.git_branch,
            git_head="h",
            clone_head="h",
            fast_tier_lag=0,
            scip_lag=0,
            scip_py_lag=0,
            state={},
            last_refresh_at="",
            counts={},
            chunks_total=0,
            chunks_embedded=0,
            embedding_coverage=0.0,
        )

    monkeypatch.setattr(index_status, "_repo_status", _status)
    index_status.reset_repo_status_cache()

    async def _allow(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(authz, "check_authorized", _allow)

    async def _state(auth: Any) -> Any:
        return {}, {}, {}, None

    monkeypatch.setattr(repos_mod, "_repo_directory_state", _state)
    nc.reset()
    set_current_auth(
        AuthInfo(
            subject="sa:x",
            org="clickhouse",
            namespaces=("jdoe",),
            roles=(Role.VIEWER,),
            is_service_account=True,
        )
    )
    yield repos_mod
    nc.reset()
    set_current_auth(None)


@pytest.mark.asyncio
async def test_code_repos_rows_name_the_repositorys_primary_namespace(plane: Any) -> None:
    from server.code.api.models_repos import CodeReposInput

    out = await plane.handle_code_repos(CodeReposInput(org="clickhouse"))
    got = {r.namespace: r.primary_namespace for r in out.repos}
    assert got == {
        "code-clickhouse-26-8": "code-clickhouse",  # a release branch reads its trunk's rules
        "code-clickhouse": "code-clickhouse",
        "code-example-private": "code-example-private",  # never the public repo's
        "code-loom": "code-loom",
    }
    # projectable like every other field
    out = await plane.handle_code_repos(
        CodeReposInput(org="clickhouse", repo="ClickHouse/loom", fields=["primary_namespace"])
    )
    assert out.repos[0].model_dump() == {"namespace": "code-loom", "primary_namespace": "code-loom"}


def test_code_repos_row_field_defaults_empty() -> None:
    from server.code.api.models_repos import CodeRepoSummary

    assert CodeRepoSummary.model_fields["primary_namespace"].default == ""
