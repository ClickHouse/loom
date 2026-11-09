"""The memory reads the code plane serves from: code.search's recent-directory
tie-break reads the user's files through memory, one parent per directory; a symbol
card's lessons are the memories linked to it, from the caller's lesson scope, each
with the state of its code on the branch read."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.domain.declaration import fingerprint_version
from server.identity.domain.read_scope import (
    WHOLE_NAMESPACE,
    ReadFilters,
    bind_request_read_filters,
)
from server.memory.app import code_plane_reads
from server.memory.store.code_links import LINK_COLS
from server.settings import settings


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        return self.rows


@pytest.mark.asyncio
async def test_user_file_dirs_are_each_parent_once_in_path_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH([{"f": "a/b/x.py"}, {"f": "a/b/y.py"}, {"f": "a/c.py"}, {"f": "top.py"}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    dirs = await code_plane_reads.user_file_dirs("o", "u", None, scope=WHOLE_NAMESPACE)
    assert dirs == ["a/b", "a", "top.py"]
    ((sql, params),) = ch.calls
    assert "FROM loom.memory FINAL" in sql and "tombstoned_at IS NULL" in sql
    assert params == {"org": "o", "uid": "u", "lim": 200}


@pytest.mark.asyncio
async def test_user_file_dirs_read_only_the_callers_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.search import handler
    from tests.unit.principals import acting_as, principal

    ch = _CH([{"f": "a/x.py"}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _mine(org: str, subject: str | None = None) -> list[str]:
        return ["ns1"]

    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _mine)
    monkeypatch.setattr(handler, "_RECENT_DIR_CACHE", {})
    with acting_as(principal("viewer", org="o", namespaces=("ns1",))):
        assert await handler._user_recent_dirs("o", "u") == ["a"]
    ((sql, params),) = ch.calls
    assert "namespace IN {nss:Array(String)}" in sql and params["nss"] == ["ns1"]
    # A caller with no readable namespace reads nothing.
    assert await code_plane_reads.user_file_dirs("o", "u", (), scope=WHOLE_NAMESPACE) == []
    assert len(ch.calls) == 1


def _recent_dirs_setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _CH]:
    from server.code.app.search import handler

    ch = _CH([{"f": "a/x.py"}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _mine(org: str, subject: str | None = None) -> list[str]:
        return ["ns1"]

    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _mine)
    monkeypatch.setattr(handler, "_RECENT_DIR_CACHE", {})
    return handler, ch


@pytest.mark.asyncio
async def test_recent_dirs_read_only_the_own_session_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.principals import acting_as, principal

    handler, ch = _recent_dirs_setup(monkeypatch)
    auth = principal("viewer", org="o", namespaces=("ns1",))
    try:
        with acting_as(auth):
            bind_request_read_filters(auth, ReadFilters("own_session", "u", "s1"))
            assert await handler._user_recent_dirs("o", "u") == ["a"]
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    ((sql, params),) = ch.calls
    assert "session_id = {scope_sid:String}" in sql
    assert params["scope_sid"] == "s1" and params["scope_uid"] == "u"


@pytest.mark.asyncio
async def test_two_sessions_never_share_a_recent_dirs_cache_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.principals import acting_as, principal

    handler, ch = _recent_dirs_setup(monkeypatch)
    auth = principal("viewer", org="o", namespaces=("ns1",))
    try:
        with acting_as(auth):
            for sid in ("s1", "s2"):
                bind_request_read_filters(auth, ReadFilters("own_session", "u", sid))
                await handler._user_recent_dirs("o", "u")
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    assert [params["scope_sid"] for _, params in ch.calls] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_an_unresolved_read_scope_personalizes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.errors import IdentityIncompleteError
    from tests.unit.principals import acting_as, principal

    handler, ch = _recent_dirs_setup(monkeypatch)
    auth = principal("viewer", org="o", namespaces=("ns1",))
    try:
        with acting_as(auth):
            bind_request_read_filters(auth, IdentityIncompleteError("no session_id"))
            assert await handler._user_recent_dirs("o", "u") == []
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    assert ch.calls == []


def test_code_search_runs_as_the_pinned_user_whatever_the_body_says() -> None:
    from server.code.api.models_search import CodeSearchInput
    from server.core.errors import AuthInsufficientError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, bind_request_read_filters
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates
    from tests.unit.principals import principal

    op = OPERATIONS_BY_NAME["code.search"]
    pinned = principal("viewer", org="o", namespaces=("ns1",), read_user_id="u1")
    try:
        out = gates.read_scope(op, pinned, CodeSearchInput(query="q"))
        assert out.user_id == "u1"  # the user whose recent files break ties
        with pytest.raises(AuthInsufficientError):
            gates.read_scope(op, pinned, CodeSearchInput(query="q", user_id="u2"))
        body = CodeSearchInput(query="q", user_id="u2")
        unpinned = principal("viewer", org="o", namespaces=("ns1",))
        assert gates.read_scope(op, unpinned, body) is body
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)


def _link(mid: str, *, qn: str = "src.a::Reader::read", state: str = "fresh", **kw: Any) -> dict:
    return LINK_COLS | {
        "memory_id": mid,
        "qualified_name": qn,
        "path": kw.get("path", "src/a.py"),
        "former": kw.get("former", []),
        "body_hash": kw.get("body", "h1"),
        "state": state,
        "changed_since": kw.get("since", ""),
        "fingerprint_version": kw.get("version", fingerprint_version()),
    }


def _excerpt(mid: str, key: str, *, pinned: int = 0) -> dict:
    return {
        "mid": mid,
        "memory_key": key,
        "namespace": "agent-a",
        "excerpt": key,
        "pinned": pinned,
        "confidence": 1.0,
    }


async def _lessons(
    monkeypatch,
    links,
    excerpts,
    defs,
    *,
    ns: str = "code-a",
    qn: str = "src.a::Reader::read",
    path: str = "src/a.py",
    parse_s: float = 0.0,
    has_mirror: bool = True,
) -> list:
    from server.code.app import memory_links
    from server.code.domain.anchor import Definition
    from server.memory.store import code_links
    from server.memory.store import code_plane_reads as store

    async def for_paths(org, cns, paths, *, include_former, namespaces):
        assert cns == ["code-a"] and paths == [path] and include_former
        return links

    async def head(org, cns):
        return f"head-of-{cns}"

    async def parsed(org, cns, commit, p, *, named=""):
        # the read namespace's own file at its own head
        assert (cns, commit) == (ns, f"head-of-{ns}")
        await asyncio.sleep(parse_s)
        here = [Definition(dp, q, q.split("::")[-1], body_hash=h) for dp, q, h in defs if dp == p]
        return memory_links.FileAt([], here)

    async def mirror_here(org, cns, commit):
        return has_mirror

    async def served(org, nss, ids):
        assert nss == ["agent-a", "code-a"]  # the lesson scope bounds the read
        return [e for e in excerpts if e["mid"] in ids]

    async def current(org, rows):
        return rows

    monkeypatch.setattr(code_links, "for_paths", for_paths)
    monkeypatch.setattr(memory_links, "indexed_head", head)
    monkeypatch.setattr(memory_links, "parsed", parsed)
    monkeypatch.setattr(memory_links, "mirror_here", mirror_here)
    monkeypatch.setattr(memory_links, "trunk_namespace", lambda org, n: "code-a")
    monkeypatch.setattr(store, "served_excerpts", served)
    monkeypatch.setattr("server.memory.app.code_link_reads.current_links", current)
    return await code_plane_reads.lessons_for_symbol(
        "o", ns, qualified_name=qn, paths=[path], namespaces=["agent-a", "code-a"]
    )


@pytest.mark.asyncio
async def test_code_gone_from_trunk_still_teaches_the_branch_that_has_it(monkeypatch) -> None:
    links = [_link("m1", state="missing", since="c3")]
    excerpts = [_excerpt("m1", "k1")]
    assert await _lessons(monkeypatch, links, excerpts, []) == []  # trunk: the code is gone
    branch = [("src/a.py", "src.a::Reader::read", "h1")]
    (lesson,) = await _lessons(monkeypatch, links, excerpts, branch, ns="code-a-26-8")
    assert (lesson["memory_key"], lesson["code_state"]) == ("k1", "fresh")


@pytest.mark.asyncio
async def test_a_lesson_reads_changed_where_the_definition_differs_from_the_linked_one(
    monkeypatch,
) -> None:
    links = [_link("m1", body="h1"), _link("m2", body="h0", state="changed", since="c2")]
    excerpts = [_excerpt("m1", "k1"), _excerpt("m2", "k2", pinned=1)]
    trunk = [("src/a.py", "src.a::Reader::read", "h1")]
    out = await _lessons(monkeypatch, links, excerpts, trunk)
    assert [(o["memory_key"], o["code_state"], o["changed_since"]) for o in out] == [
        ("k1", "fresh", ""),
        ("k2", "changed", "c2"),
    ]
    # a branch still holding the definition k2 was written against reads it fresh
    branch = [("src/a.py", "src.a::Reader::read", "h0")]
    out = await _lessons(monkeypatch, links, excerpts, branch, ns="code-a-26-8")
    assert {o["memory_key"]: o["code_state"] for o in out} == {"k1": "changed", "k2": "fresh"}


@pytest.mark.asyncio
async def test_a_slow_branch_parse_serves_the_lesson_as_trunk_has_it(monkeypatch) -> None:
    links = [_link("m1", body="h0", state="changed", since="c2") | {"suggestion": "s"}]
    branch = [("src/a.py", "src.a::Reader::read", "h0")]
    slow = settings.memory_code_plane_parse_timeout_s * 2
    (lesson,) = await _lessons(
        monkeypatch, links, [_excerpt("m1", "k1")], branch, ns="code-a-26-8", parse_s=slow
    )
    assert (lesson["code_state"], lesson["suggestion"]) == ("changed", "s")


@pytest.mark.asyncio
async def test_a_host_still_cloning_the_mirror_serves_the_lesson_as_trunk_has_it(
    monkeypatch,
) -> None:
    links = [_link("m1", body="h0", state="changed", since="c2") | {"suggestion": "s"}]
    branch = [("src/a.py", "src.a::Reader::read", "h0")]
    (lesson,) = await _lessons(
        monkeypatch, links, [_excerpt("m1", "k1")], branch, ns="code-a-26-8", has_mirror=False
    )
    assert (lesson["code_state"], lesson["suggestion"]) == ("changed", "s")


@pytest.mark.asyncio
async def test_a_lesson_is_the_symbols_own_not_a_namesakes(monkeypatch) -> None:
    links = [_link("m1", qn="src.a::Writer::read"), _link("m2"), _link("m3")]
    defs = [("src/a.py", "src.a::Reader::read", "h1"), ("src/a.py", "src.a::Writer::read", "h1")]
    out = await _lessons(monkeypatch, links, [_excerpt("m1", "k1"), _excerpt("m2", "k2")], defs)
    assert [(o["memory_key"], o["code_state"]) for o in out] == [("k2", "fresh")]


@pytest.mark.asyncio
async def test_a_lesson_keeps_the_namespace_and_the_enclosing_type(monkeypatch) -> None:
    path = "src/reader.cpp"
    links = [
        _link("m1", qn="first::Reader::read_rows", path=path),
        _link("m2", qn="second::Reader::read_rows", path=path),
    ]
    defs = [(path, "first::Reader::read_rows", "h1"), (path, "second::Reader::read_rows", "h1")]
    out = await _lessons(
        monkeypatch,
        links,
        [_excerpt("m1", "k1"), _excerpt("m2", "k2")],
        defs,
        qn="first::Reader::read_rows",
        path=path,
    )
    assert [o["memory_key"] for o in out] == ["k1"]


@pytest.mark.asyncio
async def test_the_compiler_tiers_module_name_still_finds_the_definition(monkeypatch) -> None:
    # the compiler tier names the module `a`; tree-sitter names it by its path `src.a`
    defs = [("src/a.py", "src.a::Reader::read", "h1"), ("src/a.py", "src.a::read", "h1")]
    links = [_link("m1"), _link("m2", qn="src.a::read")]
    excerpts = [_excerpt("m1", "k1"), _excerpt("m2", "k2")]
    out = await _lessons(monkeypatch, links, excerpts, defs, qn="a.Reader.read")
    assert [o["memory_key"] for o in out] == ["k1"]  # not the module's own `read`


@pytest.mark.asyncio
async def test_a_branch_without_the_move_still_gets_the_lesson(
    monkeypatch,
) -> None:
    moved = _link(
        "m1",
        qn="src.b::Reader::read",
        path="src/b.py",
        former=[{"path": "src/a.py", "name": "src.a::Reader::read", "hash": "h1"}],
        state="moved",
    )
    branch = [("src/a.py", "src.a::Reader::read", "h1")]
    out = await _lessons(monkeypatch, [moved], [_excerpt("m1", "k1")], branch, ns="code-a-26-8")
    assert {o["memory_key"]: o["code_state"] for o in out} == {"k1": "fresh"}


@pytest.mark.asyncio
async def test_the_lesson_scope_is_the_callers_own_the_teams_and_the_readable_extras(
    monkeypatch,
) -> None:
    from server.auth import authz
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    async def readable(org, ns, role, *, hides_answer=True):
        return ns not in ("secret", "code-a")

    monkeypatch.setattr(authz, "row_in_scope", readable)
    monkeypatch.setattr("server.code.app.memory_links.trunk_namespace", lambda org, n: "code-a")
    set_current_auth(
        AuthInfo(
            subject="alice@x.io",
            org="o",
            namespaces=("team-b", "alice", "bob"),
            roles=(Role.WRITER,),
            is_service_account=False,
        )
    )
    try:
        nss = await code_plane_reads.lesson_namespaces("o", "code-a-26-8", ["alice", "secret"])
    finally:
        set_current_auth(None)
    # only what the call names (over MCP: the caller's memory home), never a guess from
    # the token; the trunk too only when the caller may read it
    assert nss == ["alice"]


@pytest.mark.asyncio
async def test_lessons_keep_the_named_order_and_read_nothing_when_none_is_readable(
    monkeypatch,
) -> None:
    from server.auth import authz
    from server.code.app import memory_links
    from server.settings import settings

    hidden = {"secret"}

    async def readable(org, ns, role, *, hides_answer=True):
        return ns not in hidden

    monkeypatch.setattr(authz, "row_in_scope", readable)
    monkeypatch.setattr(memory_links, "trunk_namespace", lambda org, n: "code-a")
    nss = await code_plane_reads.lesson_namespaces("o", "code-a-26-8", ["bob", "secret", "alice"])
    assert nss == [settings.default_namespace, "bob", "alice", "code-a"]
    hidden.update(nss)
    assert await code_plane_reads.lesson_namespaces("o", "code-a", ["bob"]) == []

    async def no_read(*a, **k):
        raise AssertionError("no lesson namespace is readable: nothing is read")

    monkeypatch.setattr(code_plane_reads.links_store, "for_paths", no_read)
    monkeypatch.setattr(memory_links, "indexed_head", no_read)
    out = await code_plane_reads.lessons_for_symbol(
        "o", "code-a", qualified_name="a::f", paths=["a.py"], namespaces=[]
    )
    assert out == []


async def test_an_mcp_code_read_names_the_callers_memory_home_for_its_lessons(monkeypatch) -> None:
    from server.transport import mcp_call

    async def defaults(auth, client_name=None):
        return {"org": "o", "namespace": "agent-alice-claude", "agent": "claude-code"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", defaults)
    from server.operations.registry import OPERATIONS_BY_NAME

    arguments: dict = {"name": "Reader.read"}
    await mcp_call._bind_caller_identity(OPERATIONS_BY_NAME["code.symbol"], arguments, None, None)
    assert arguments["lesson_namespaces"] == ["agent-alice-claude"]
    named = {"name": "Reader.read", "lesson_namespaces": ["team-x"]}
    await mcp_call._bind_caller_identity(OPERATIONS_BY_NAME["code.symbol"], named, None, None)
    assert named["lesson_namespaces"] == ["team-x"]


def test_a_link_hashed_under_other_rules_reads_its_stored_state_until_rehashed() -> None:
    from server.code.domain.anchor import Definition
    from server.memory.app.code_plane_reads import _lesson_state

    now = Definition("src/a.py", "src.a::Reader::read", "read", body_hash="h-new-rules")
    old = _link("m1", body="h-old-rules", version="0-old")
    assert _lesson_state(old, now, at_current=True, at_anchor=False, on_trunk=True) == "fresh"
    assert (
        _lesson_state(_link("m1", body="h1"), now, at_current=True, at_anchor=False, on_trunk=True)
        == "changed"
    )
    carried = _link("m1", body="h-new-rules") | {"carried": 1}
    assert (
        _lesson_state(carried, now, at_current=True, at_anchor=False, on_trunk=False) == "changed"
    )


def test_a_branch_reads_the_anchored_text_only_at_the_anchored_definition() -> None:
    from server.code.domain.anchor import Definition
    from server.memory.app.code_plane_reads import _lesson_state

    # Reader (limit 3) was renamed to Writer (limit 4), then to Loader (limit 5): the
    # method's own text never changed, its class did
    original = Definition("src/a.py", "src.a::Reader::fetch_rows", "fetch_rows", body_hash="h")
    between = Definition("src/a.py", "src.a::Writer::fetch_rows", "fetch_rows", body_hash="h")
    link = _link("m1", body="h") | {"carried": 1, "anchored_hash": "h", "state": "changed"}

    def state(now, at_anchor):
        return _lesson_state(link, now, at_current=False, at_anchor=at_anchor, on_trunk=False)

    assert state(original, at_anchor=True) == "fresh"  # a branch that still has Reader
    assert state(between, at_anchor=False) == "changed"  # one that has Writer
    # and without a carry, the anchored text counts only where it was anchored
    plain = _link("m1", body="h-edited") | {"anchored_hash": "h"}
    assert (
        _lesson_state(plain, original, at_current=False, at_anchor=True, on_trunk=False) == "fresh"
    )
    assert (
        _lesson_state(plain, between, at_current=False, at_anchor=False, on_trunk=False)
        == "changed"
    )


@pytest.mark.asyncio
async def test_a_lesson_read_whose_parse_gave_up_waiting_is_read_as_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parser that takes a refusal for the file's own answer (a fallback, no error) does
    not turn a busy parse lock into a branch without the symbol."""
    import tree_sitter
    import tree_sitter_go

    from server.code.app import memory_links
    from server.code.domain.extract import bounded_parse

    parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_go.language()))

    def swallowing(path: str, data: bytes, named: str) -> Any:
        try:
            bounded_parse.parse(parser, data)
        except bounded_parse.ParseTimeout:
            pass  # read as a file that defines nothing
        return memory_links.FileAt([], [])

    async def mirror_here(org: str, cns: str, commit: str) -> bool:
        return True

    async def file_bytes(org: str, cns: str, commit: str, path: str) -> bytes:
        return b"package x\nfunc f() {}\n"

    monkeypatch.setattr(memory_links, "mirror_here", mirror_here)
    monkeypatch.setattr(memory_links, "_file_at", file_bytes)
    monkeypatch.setattr(memory_links, "file_at", swallowing)
    monkeypatch.setattr(settings, "memory_code_plane_parse_timeout_s", 5.0)
    real = bounded_parse.LockWait
    # the parse gives up waiting well before the read does
    monkeypatch.setattr(bounded_parse, "LockWait", lambda seconds: real(0.05))
    with bounded_parse._LOCK:  # another parse holds the process's turn
        here = await code_plane_reads._definitions_at("o", "code-a", "h", ["a.go"], "f")
    assert here is None
