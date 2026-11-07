"""Code-linked memories end to end against a live ClickHouse: a real git repository is
indexed commit by commit, memories are written through memory.set, and the link pass
links them, follows a rename, flags a changed body, leaves a memory whose code is gone
out of search (and serves it again when the code comes back), honours the namespace
policy and the person's holds, resolves a name once it is merged, and serves the links
on code.symbol, memory.for_file, memory.get and memory.search, branch by branch. Loom
never writes the memory itself.

Handlers run in-process; skips without LOOM_CH_HOST (house rule).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

ORG = unique_org("codelinks")
REMOTE = "https://github.com/acme/widgets.git"
# Namespaces of the running test (the env fixture draws fresh ones per test).
TRUNK = BRANCH = MEM = FLAGGED = ""
# The queue's newest write when the test started: the suite shares one queue, and every
# other test's writes come before this test's own.
QUEUE_START: Any = None
# the backfill cursor once a scan is done
_BACKFILL_DONE = '{"done": true}'

_READER = """class Reader:
    def read_rows(self, limit):
        return limit


def fetch_page(url):
    return url
"""
_UTIL = """def unique_helper():
    return 1
"""
_FUTURE = """class Planner:
    def plan_route(self):
        return []
"""


class _Repo:
    """A git working tree whose `.git` is the mirror the index reads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "it@example.com")
        self._git("config", "user.name", "it")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, files: dict[str, str | None], *, mv: tuple[str, str] | None = None) -> str:
        if mv:
            (self.root / mv[1]).parent.mkdir(parents=True, exist_ok=True)
            self._git("mv", *mv)
        for path, text in files.items():
            p = self.root / path
            if text is None:
                self._git("rm", "-q", path)
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            self._git("add", path)
        self._git("commit", "-q", "-m", "change")
        return self._git("rev-parse", "HEAD")


async def _index(ns: str, repo: _Repo, sha: str, paths: list[str]) -> None:
    """What the ingest worker does for one commit: re-ingest the changed paths (a path no
    longer in the tree is tombstoned) and advance the namespace's head."""
    from server.code.app.index.ingest import ingest_file, tombstone_file
    from server.code.store.index import CodeRepo
    from server.embedding.fake import FakeNGramEmbedder
    from server.settings import settings

    code = CodeRepo()
    for path in paths:
        p = repo.root / path
        if p.exists():
            await ingest_file(
                code,
                ORG,
                ns,
                path=path,
                source=p.read_bytes(),
                commit_sha=sha,
                embedding_model="fake-ngram-1024",
                embedder=FakeNGramEmbedder(dim=settings.embedding_dim),
            )
        else:
            await tombstone_file(code, ORG, ns, path=path, embedding_model="fake-ngram-1024")
    await code.set_state(ORG, ns, "git_head", sha)


@pytest.fixture
async def env(ch_client, tmp_path, monkeypatch):
    global TRUNK, BRANCH, MEM, FLAGGED, QUEUE_START
    import server.code.app.memory_links as code_side
    from server.code.domain.index.repos import RepoConfig, set_registry_repos
    from server.core.ch import get_client
    from server.settings import settings

    await get_client().connect()
    from server.memory.store import code_links as link_store

    QUEUE_START = await link_store.queue_tail()
    tag = os.urandom(3).hex()
    TRUNK, MEM, FLAGGED = f"code-widgets-{tag}", f"agent-links-{tag}", f"agent-flag-{tag}"
    BRANCH = f"{TRUNK}-release"
    repo = _Repo(tmp_path / "widgets")
    git_dir = str(repo.root / ".git")
    entries = [
        RepoConfig(
            org=ORG,
            namespace=ns,
            repo="acme/widgets",
            git_dir=git_dir,
            git_remote=REMOTE,
            git_branch=branch,
            path_prefixes=("",),
            github_token="",
            source="registry",
        )
        for ns, branch in ((TRUNK, "main"), (BRANCH, "release"))
    ]
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    # the writes below land at once; no in-flight write to wait for
    monkeypatch.setattr(settings, "memory_code_links_settle_seconds", 0)
    set_registry_repos(entries)

    async def _no_refresh() -> None:
        return None

    monkeypatch.setattr(code_side, "refresh_repos", _no_refresh)
    yield repo
    set_registry_repos([])


async def _remember(namespace: str, key: str, value: str, **kw: Any) -> str:
    from server.code.domain.index.remote import repo_tag
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    out = await handle_memory_set(
        MemorySetInput(
            org=ORG,
            namespace=namespace,
            agent=kw.pop("agent", "claude-code"),
            memory_key=key,
            value=value,
            tags=[repo_tag(REMOTE), *kw.pop("tags", [])],
            **kw,
        )
    )
    return str(out.memory_id)


async def _set_policy(namespace: str, policy: str) -> None:
    """namespace.register as the namespace's owner, setting its code_change_policy."""
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models import NamespaceGetInput, NamespaceRegisterInput
    from server.identity.app.namespace import handle_namespace_get, handle_namespace_register
    from server.operations.base import Role

    set_current_auth(
        AuthInfo(
            subject="it@example.com",
            org=ORG,
            namespaces=(namespace,),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    try:
        await handle_namespace_register(
            NamespaceRegisterInput(
                org=ORG,
                namespace=namespace,
                owner_email="it@example.com",
                code_change_policy=policy,
            )
        )
        got = await handle_namespace_get(NamespaceGetInput(org=ORG, namespace=namespace))
        assert got.code_change_policy == policy
    finally:
        set_current_auth(None)


async def _pass() -> Any:
    from server.memory.app import code_link_pass, code_link_recheck, code_links

    result = code_link_pass.PassResult()
    cursor = QUEUE_START
    while True:
        read = result.queued
        cursor = await code_links.link_queued(cursor, 10_000, result)
        if result.queued - read < 10_000:
            break
    await code_link_recheck.recheck_all(result)
    assert not result.errors, result.errors
    return result


async def _search(namespace: str, query: str, **kw: Any) -> dict[str, str]:
    """memory_key -> code_state of what memory.search serves."""
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search

    found = await handle_memory_search(
        MemorySearchInput(org=ORG, namespace=namespace, agent="*", query=query, **kw)
    )
    return {h.memory_key: h.code_state for h in found.results}


async def _links(memory_id: str) -> list[dict[str, str]]:
    from server.memory.app.code_link_reads import links_of
    from server.memory.store.code_links import memory_heads

    heads = await memory_heads(ORG, [memory_id])
    return [link.model_dump() for link in await links_of(ORG, heads[0])] if heads else []


async def _row(memory_id: str) -> Any:
    import server.memory.store as memory_repo
    from server.identity.domain.read_scope import WHOLE_NAMESPACE

    return await memory_repo.get_by_id(memory_id, scope=WHOLE_NAMESPACE, org=ORG, strong=True)


async def _lessons(ns: str, name: str) -> list[dict[str, Any]]:
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app.symbol import handle_code_symbol
    from server.settings import settings

    settings_ns = settings.default_namespace
    try:
        settings.default_namespace = MEM
        out = await handle_code_symbol(
            CodeSymbolInput(
                org=ORG,
                namespace=ns,
                name=name,
                include_body=False,
                include_history=False,
                include_examples=False,
            )
        )
    finally:
        settings.default_namespace = settings_ns
    return list(out.definitions[0].lessons) if out.definitions else []


async def test_memories_follow_their_code(env, monkeypatch) -> None:
    from server.memory.app import code_link_judge

    repo = env
    c1 = repo.commit({"src/widgets/reader.py": _READER, "src/widgets/util.py": _UTIL})
    for ns in (TRUNK, BRANCH):
        await _index(ns, repo, c1, ["src/widgets/reader.py", "src/widgets/util.py"])
    await _set_policy(FLAGGED, "flag")

    leak = await _remember(
        MEM, "reader-leak", "`Reader.read_rows` leaks its buffer when called twice"
    )
    retry = await _remember(
        MEM, "fetch-retry", "fetch_page retries on code 115", files=["src/widgets/reader.py"]
    )
    rule = await _remember(
        TRUNK, "util-generated", "never edit it by hand", files=["src/widgets/util.py"]
    )
    pinned = await _remember(MEM, "helper-pinned", "`unique_helper` returns one", pinned=True)
    flagged = await _remember(FLAGGED, "helper-flagged", "`unique_helper` is the only id source")
    later = await _remember(
        MEM, "planner", "`widgets.future.Planner.plan_route` replaces the fetch loop"
    )
    prose = await _remember(MEM, "prose", "the users were happy with the result")

    await _pass()
    assert [(link["symbol"].rsplit("::", 1)[-1], link["state"]) for link in await _links(leak)] == [
        ("read_rows", "fresh")
    ]
    assert [link["symbol"].rsplit("::", 1)[-1] for link in await _links(retry)] == ["fetch_page"]
    assert [(link["symbol"], link["path"]) for link in await _links(rule)] == [
        ("", "src/widgets/util.py")
    ]
    assert await _links(prose) == [] and await _links(later) == []  # pending names are not served

    # The read side: the symbol card, the file hook, the memory itself
    lessons = await _lessons(TRUNK, "Reader.read_rows")
    assert [(lesson["memory_key"], lesson["code_state"]) for lesson in lessons] == [
        ("reader-leak", "fresh")
    ]
    from server.memory.api.models_for_file import MemoryForFileInput
    from server.memory.app.for_file import handle_memory_for_file

    hook = await handle_memory_for_file(
        MemoryForFileInput(
            org=ORG, namespace=MEM, path="src/widgets/reader.py", extra_namespaces=[TRUNK]
        )
    )
    assert {h.memory_key for h in hook.hits} == {"reader-leak", "fetch-retry"}

    # c2: reader.py renamed (fetch_page also edited), util.py deleted, future.py merged
    c2 = repo.commit(
        {
            "src/widgets/readers.py": _READER.replace("return url", "return url.strip()"),
            "src/widgets/util.py": None,
            "src/widgets/future.py": _FUTURE,
        },
        mv=("src/widgets/reader.py", "src/widgets/readers.py"),
    )
    await _index(
        TRUNK,
        repo,
        c2,
        [
            "src/widgets/reader.py",
            "src/widgets/readers.py",
            "src/widgets/util.py",
            "src/widgets/future.py",
        ],
    )
    await _pass()

    (moved,) = await _links(leak)
    assert (moved["path"], moved["state"]) == ("src/widgets/readers.py", "moved")
    (changed,) = await _links(retry)
    assert (changed["path"], changed["state"], changed["changed_since"]) == (
        "src/widgets/readers.py",
        "changed",
        c2,
    )
    # the team rule's file is gone: search leaves it out; the pinned and the flagged
    # memories are served, marked; Loom wrote none of them
    assert [link["state"] for link in await _links(rule)] == ["missing"]
    assert "util-generated" not in await _search(TRUNK, "never edit it by hand")
    assert (await _search(MEM, "unique_helper returns one")).get("helper-pinned") == "missing"
    flagged_hits = await _search(FLAGGED, "unique_helper is the only id source")
    assert flagged_hits.get("helper-flagged") == "missing"
    for mid in (rule, pinned, flagged):
        assert (await _row(mid)).status == "active"
    assert [link["symbol"].rsplit("::", 1)[-1] for link in await _links(later)] == ["plan_route"]

    # On trunk the changed claim reads changed; the release branch still has the body the
    # memory was written against, so there it reads fresh.
    assert [lesson["code_state"] for lesson in await _lessons(TRUNK, "fetch_page")] == ["changed"]
    assert [lesson["code_state"] for lesson in await _lessons(BRANCH, "fetch_page")] == ["fresh"]

    # memory.get carries the links, memory.search the worst state, namespace.validate the
    # served memories whose code changed or went away
    from server.identity.api.models_namespace_catalog import NamespaceValidateInput
    from server.identity.app.namespace_validate import handle_namespace_validate
    from server.memory.api.models_crud import MemoryGetInput
    from server.memory.app.handlers import handle_memory_get

    got = await handle_memory_get(
        MemoryGetInput(org=ORG, namespace=MEM, agent="claude-code", memory_key="fetch-retry")
    )
    assert [(c.path, c.state) for c in got.code] == [("src/widgets/readers.py", "changed")]
    states = await _search(MEM, "fetch_page retries on code 115")
    assert states.get("fetch-retry") == "changed", states
    report = await handle_namespace_validate(NamespaceValidateInput(org=ORG, namespace=MEM))
    stale = next(f for f in report.findings if f.check == "code_links_stale")
    assert {s["memory_key"] for s in stale.samples} == {"fetch-retry", "helper-pinned"}
    assert stale.count == 2
    report = await handle_namespace_validate(
        NamespaceValidateInput(org=ORG, namespace=MEM, keys=["helper-pinned"])
    )
    stale = next(f for f in report.findings if f.check == "code_links_stale")
    assert [s["memory_key"] for s in stale.samples] == ["helper-pinned"] and stale.count == 1

    # c3: util.py comes back, and with it the rule
    c3 = repo.commit({"src/widgets/util.py": _UTIL})
    await _index(TRUNK, repo, c3, ["src/widgets/util.py"])
    await _pass()
    assert [link["state"] for link in await _links(rule)] == ["fresh"]
    assert "util-generated" in await _search(TRUNK, "never edit it by hand")

    # A rewritten memory is relinked from its new text
    await _remember(MEM, "reader-leak", "`fetch_page` must strip the url")
    await _pass()
    assert [link["symbol"].rsplit("::", 1)[-1] for link in await _links(leak)] == ["fetch_page"]

    # revise: a contradicted claim gets a suggested correction; its text is untouched
    await _set_policy(MEM, "revise")

    async def judge(claim: str, before: str, after: str) -> tuple[str, str]:
        if claim != "`fetch_page` must strip the url":
            return "unsure", ""  # another memory about fetch_page, edited again
        assert "strip" in before and "lower" in after
        return "contradicted", "fetch_page lower-cases the url"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    c4 = repo.commit(
        {"src/widgets/readers.py": _READER.replace("return url", "return url.lower()")}
    )
    await _index(TRUNK, repo, c4, ["src/widgets/readers.py"])
    result = await _pass()
    assert result.suggested >= 1
    (link,) = await _links(leak)
    assert (link["state"], link["suggestion"]) == ("changed", "fetch_page lower-cases the url")
    assert (await _row(leak)).value == "`fetch_page` must strip the url"


async def test_erasure_removes_the_links(env) -> None:
    from server.erasure.store.steps_memory import delete_code_links_by_namespace

    repo = env
    c1 = repo.commit({"src/widgets/reader.py": _READER})
    await _index(TRUNK, repo, c1, ["src/widgets/reader.py"])
    mid = await _remember(MEM, "erase-me", "`Reader.read_rows` is hot")
    await _pass()
    assert await _links(mid)
    await delete_code_links_by_namespace(ORG, MEM)
    assert await _links(mid) == []


async def test_a_rename_alone_moves_the_link(env) -> None:
    repo = env
    src = "def fetch_rows(conn, n):\n    rows = conn.read(n)\n    return [r for r in rows if r]\n"
    c1 = repo.commit({"src/widgets/io.py": src})
    await _index(TRUNK, repo, c1, ["src/widgets/io.py"])
    mid = await _remember(MEM, "fetch-rows", "`fetch_rows` drops empty rows")
    await _pass()
    c2 = repo.commit({"src/widgets/io.py": src.replace("fetch_rows", "load_rows")})
    await _index(TRUNK, repo, c2, ["src/widgets/io.py"])
    await _pass()
    (link,) = await _links(mid)
    assert (link["symbol"].rsplit("::", 1)[-1], link["state"]) == ("load_rows", "moved")


async def test_a_file_named_by_its_basename_resolves_to_its_one_live_path(env) -> None:
    from server.code.app.memory_links import live_paths

    repo = env
    files = {"src/widgets/reader.py": _READER, "src/widgets/util.py": _UTIL, "lib/util.py": _UTIL}
    c1 = repo.commit(files)
    await _index(TRUNK, repo, c1, list(files))
    refs = ["reader.py", "util.py", "absent.py", "lib/util.py"]
    assert await live_paths(ORG, TRUNK, refs) == {
        "reader.py": "src/widgets/reader.py",
        "lib/util.py": "lib/util.py",
    }


async def test_an_edit_deep_in_a_long_definition_or_to_any_overload_reads_changed(env) -> None:
    repo = env
    body = "".join(f"    total += step({i}) * {i}\n" for i in range(400))
    long_src = f"def crunch(step):\n    total = 0\n{body}    return total\n"
    cpp = (
        "namespace DB {\nclass Sink {\npublic:\n"
        "    void insert(int v) { count += v; }\n"
        "    void insert(double v) { sum += v; }\n};\n}\n"
    )
    c1 = repo.commit({"src/widgets/crunch.py": long_src, "src/Sink.cpp": cpp})
    await _index(TRUNK, repo, c1, ["src/widgets/crunch.py", "src/Sink.cpp"])
    deep = await _remember(MEM, "crunch", "`crunch` sums every weighted step")
    over = await _remember(MEM, "sink", "`Sink::insert` only accumulates", files=["src/Sink.cpp"])
    await _pass()
    assert [link["state"] for link in await _links(deep)] == ["fresh"]
    assert [link["state"] for link in await _links(over)] == ["fresh"]
    c2 = repo.commit(
        {
            "src/widgets/crunch.py": long_src.replace("step(399) * 399", "step(399) * 0"),
            "src/Sink.cpp": cpp.replace("sum += v;", "sum -= v;"),
        }
    )
    await _index(TRUNK, repo, c2, ["src/widgets/crunch.py", "src/Sink.cpp"])
    await _pass()
    assert [link["state"] for link in await _links(deep)] == ["changed"]
    assert [link["state"] for link in await _links(over)] == ["changed"]


async def test_new_files_relink_and_a_deleted_memory_drops_its_links(env) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.app.handlers import handle_memory_delete

    repo = env
    c1 = repo.commit(
        {"src/a.py": "def alpha():\n    return 1\n", "src/b.py": "def beta():\n    return 2\n"}
    )
    await _index(TRUNK, repo, c1, ["src/a.py", "src/b.py"])
    mid = await _remember(MEM, "file-rule", "keep this module tiny", files=["src/a.py"])
    await _pass()
    assert [link["path"] for link in await _links(mid)] == ["src/a.py"]
    await _remember(MEM, "file-rule", "keep this module tiny", files=["src/b.py"])
    await _pass()
    assert [link["path"] for link in await _links(mid)] == ["src/b.py"]
    c2 = repo.commit({"src/b.py": None})
    await _index(TRUNK, repo, c2, ["src/b.py"])
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["missing"]
    await handle_memory_delete(
        MemoryDeleteInput(org=ORG, namespace=MEM, agent="claude-code", memory_key="file-rule")
    )
    await _pass()
    assert await _links(mid) == []


async def test_revise_skips_what_it_cannot_judge_and_a_holding_claim_re_anchors(
    env, monkeypatch, ch_client
) -> None:
    from server.memory.app import code_link_judge

    repo = env
    src = "def ping(host):\n    return host\n"
    c1 = repo.commit({"src/net.py": src})
    await _index(TRUNK, repo, c1, ["src/net.py"])
    await _set_policy(MEM, "revise")
    holds = await _remember(MEM, "ping", "`ping` returns the host")
    long_claim = "`ping` returns the host unchanged. " + "context " * 400
    long_one = await _remember(MEM, "ping-long", long_claim)
    await _pass()
    calls: list[str] = []

    async def judge(claim: str, before: str, after: str) -> tuple[str, str]:
        calls.append(claim)
        return "holds", ""

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    c2 = repo.commit({"src/net.py": src.replace("return host", "return host.lower()")})
    await _index(TRUNK, repo, c2, ["src/net.py"])
    await _pass()
    assert calls == ["`ping` returns the host"]  # the long claim is never judged
    (link,) = await _links(holds)
    assert link["state"] == "fresh"  # re-anchored on the new code
    assert [x["state"] for x in await _links(long_one)] == ["changed"]
    assert (await _row(long_one)).value == long_claim


async def test_a_write_landing_after_later_ones_is_still_linked(env) -> None:
    """A writer takes its version before it embeds; one that lands after the cursor moved
    past later writes is read by when it was inserted."""
    import server.memory.store as memory_repo
    from server.memory.app import code_link_pass, code_links

    repo = env
    c1 = repo.commit({"src/late.py": "def late_value():\n    return 9\n"})
    await _index(TRUNK, repo, c1, ["src/late.py"])
    mid = await _remember(MEM, "early", "plain words first")
    early_version = memory_repo.next_version()  # a writer takes its version, then embeds
    await _remember(MEM, "later", "an unrelated later write")
    first = code_link_pass.PassResult()
    cursor = await code_links.link_queued(QUEUE_START, 10_000, first)  # past both
    head = await _row(mid)
    row = memory_repo.build_memory_version_row(
        head, value='"`late_value` is nine"', content_sha256="late", version=early_version
    )
    await memory_repo.insert_memory_versions([row])  # lands now, with an older version
    second = code_link_pass.PassResult()
    await code_links.link_queued(cursor, 10_000, second)
    assert [link["symbol"].rsplit("::", 1)[-1] for link in await _links(mid)] == ["late_value"]


async def test_a_memory_written_before_the_first_index_links_once_it_exists(env) -> None:
    repo = env
    mid = await _remember(MEM, "early-bird", "`first_value` returns one")
    await _pass()
    assert await _links(mid) == []
    c1 = repo.commit({"src/first.py": "def first_value():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/first.py"])
    await _pass()
    assert [(x["symbol"].rsplit("::", 1)[-1], x["state"]) for x in await _links(mid)] == [
        ("first_value", "fresh")
    ]


async def test_removed_code_that_returns_elsewhere_is_found_and_served_again(env) -> None:
    repo = env
    cpp = "namespace DB {\nint fetch_rows(int n) { return n; }\n}\n"
    c1 = repo.commit({"src/A.cpp": cpp})
    await _index(TRUNK, repo, c1, ["src/A.cpp"])
    mid = await _remember(MEM, "fetch", "`DB::fetch_rows` returns its argument")
    await _pass()
    c2 = repo.commit({"src/A.cpp": None})
    await _index(TRUNK, repo, c2, ["src/A.cpp"])
    await _pass()
    assert "fetch" not in await _search(MEM, "DB::fetch_rows returns its argument")
    c3 = repo.commit({"src/B.cpp": cpp})
    await _index(TRUNK, repo, c3, ["src/B.cpp"])
    await _pass()
    (link,) = await _links(mid)
    assert (link["path"], link["state"]) == ("src/B.cpp", "moved")
    assert "fetch" in await _search(MEM, "DB::fetch_rows returns its argument")


async def test_code_gone_past_the_search_window_returns_only_in_its_own_file(
    env, ch_client
) -> None:
    repo = env
    cpp = "namespace DB {\nint fetch_rows(int n) { return n; }\n}\n"
    c1 = repo.commit({"src/A.cpp": cpp})
    await _index(TRUNK, repo, c1, ["src/A.cpp"])
    mid = await _remember(MEM, "fetch", "`DB::fetch_rows` returns its argument")
    await _pass()
    c2 = repo.commit({"src/A.cpp": None})
    await _index(TRUNK, repo, c2, ["src/A.cpp"])
    await _pass()
    ch_client.command(
        "ALTER TABLE loom.memory_code_links UPDATE checked_at = now() - INTERVAL 31 DAY "
        "WHERE org = {o:String} AND namespace = {n:String} SETTINGS mutations_sync = 2",
        parameters={"o": ORG, "n": MEM},
    )
    c3 = repo.commit({"src/B.cpp": cpp})
    await _index(TRUNK, repo, c3, ["src/B.cpp"])
    await _pass()
    assert [(x["path"], x["state"]) for x in await _links(mid)] == [("src/A.cpp", "missing")]
    c4 = repo.commit({"src/A.cpp": cpp})
    await _index(TRUNK, repo, c4, ["src/A.cpp"])
    await _pass()
    assert [(x["path"], x["state"]) for x in await _links(mid)] == [("src/A.cpp", "fresh")]


async def test_the_sweep_removes_every_version_of_an_old_tombstone(env, ch_client) -> None:
    from server.memory.store import code_links as store

    c1 = env.commit(
        {"src/t.py": "def first_rows():\n    return 1\n\n\ndef second_rows():\n    return 2\n"}
    )
    await _index(TRUNK, env, c1, ["src/t.py"])
    mid = await _remember(MEM, "t", "`first_rows` returns one")
    await _pass()
    await _remember(MEM, "t", "`second_rows` returns two")  # the first link is tombstoned
    await _pass()

    def versions() -> int:
        return ch_client.query(
            "SELECT count() FROM loom.memory_code_links WHERE memory_id = {m:UUID} "
            "AND qualified_name LIKE '%first_rows'",
            parameters={"m": mid},
        ).result_rows[0][0]

    assert versions() >= 1  # its tombstone, over the live row unless a merge folded them
    ch_client.command(
        "ALTER TABLE loom.memory_code_links UPDATE tombstoned_at = now() - INTERVAL 8 DAY "
        "WHERE memory_id = {m:UUID} AND tombstoned_at IS NOT NULL SETTINGS mutations_sync = 2",
        parameters={"m": mid},
    )
    after: tuple[str, ...] | None = (ORG, TRUNK, "", "", "", "00000000-0000-0000-0000-000000000000")
    while after is not None and after[:2] == (ORG, TRUNK):
        after, _ = await store.sweep_page(after, 1000)
    assert versions() == 0
    assert [x["symbol"].rsplit("::", 1)[-1] for x in await _links(mid)] == ["second_rows"]


async def test_a_bulk_write_larger_than_a_page_is_linked_whole(env) -> None:
    """One bulk insert shares one queue time; pages split it by position, never by time."""
    from server.code.domain.index.remote import repo_tag
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
    from server.memory.app import code_link_pass, code_links
    from server.memory.app.bulk_set import handle_memory_bulk_set

    repo = env
    c1 = repo.commit({"src/bulk.py": "def bulk_value():\n    return 7\n"})
    await _index(TRUNK, repo, c1, ["src/bulk.py"])
    entries = [
        _BulkSetEntry(
            memory_key=f"bulk-{i}", value=f"`bulk_value` fact {i}", tags=[repo_tag(REMOTE)]
        )
        for i in range(12)
    ]
    from server.memory.store.code_links import queue_tail

    cursor = await queue_tail()  # other tests' writes are queued before this one
    await handle_memory_bulk_set(
        MemoryBulkSetInput(org=ORG, namespace=MEM, agent="claude-code", entries=entries)
    )
    result = code_link_pass.PassResult()
    for _ in range(10):
        nxt = await code_links.link_queued(cursor, 5, result)
        if nxt == cursor:
            break
        cursor = nxt
    links = await _links_by_key(MEM)
    assert sorted(links) == sorted(f"bulk-{i}" for i in range(12))


async def _links_by_key(namespace: str) -> dict[str, list[dict[str, Any]]]:
    from server.core.ch import get_client

    rows = await get_client().query_all(
        "SELECT * FROM loom.memory_code_links FINAL WHERE tombstoned_at IS NULL "
        "AND org = {org:String} AND namespace = {ns:String}",
        params={"org": ORG, "ns": namespace},
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for a in rows:
        out.setdefault(str(a["memory_key"]), []).append(a)
    return out


async def test_a_deletion_half_ingested_is_not_read_as_the_heads(env) -> None:
    from server.code.app.index.ingest import tombstone_file
    from server.code.store.index import CodeRepo

    repo = env
    src = "def kept_value():\n    return 4\n"
    c1 = repo.commit({"src/k.py": src})
    await _index(TRUNK, repo, c1, ["src/k.py"])
    sym = await _remember(MEM, "kept", "`kept_value` is four")
    rule = await _remember(MEM, "kept-file", "keep it short", files=["src/k.py"])
    await _pass()
    c2 = repo.commit({"src/k.py": src + "\n# touched\n"})
    await _index(TRUNK, repo, c2, ["src/k.py"])
    repo.commit({"src/k.py": None})  # c3: its ingest has started, git_head is still c2
    await tombstone_file(CodeRepo(), ORG, TRUNK, path="src/k.py", embedding_model="fake-ngram-1024")
    await _pass()
    # c2 has the file: git, not the half-ingested index, says so
    assert [link["state"] for link in await _links(sym)] == ["fresh"]
    assert [link["state"] for link in await _links(rule)] == ["fresh"]


async def test_a_hit_carries_the_code_state_of_its_own_memory_only(env) -> None:
    repo = env
    src = "def fetch_lunch_rows():\n    return 1\n"
    c1 = repo.commit({"src/l.py": src})
    await _index(TRUNK, repo, c1, ["src/l.py"])
    await _remember(MEM, "lunch", "alice likes ramen for lunch", user_id="alice")
    await _remember(MEM, "lunch", "`fetch_lunch_rows` returns one", user_id="bob")
    await _pass()
    c2 = repo.commit({"src/l.py": src.replace("return 1", "return 2")})
    await _index(TRUNK, repo, c2, ["src/l.py"])
    await _pass()
    assert (await _search(MEM, "lunch", user_id="alice")).get("lunch") == ""
    assert (await _search(MEM, "fetch_lunch_rows", user_id="bob")).get("lunch") == "changed"
    own = await _search(MEM, "fetch_lunch_rows", user_id="bob", read_scope="own_user")
    assert own.get("lunch") == "changed"


async def test_a_key_two_agents_share_carries_each_ones_own_code_state(env) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search

    repo = env
    src = "def fetch_dinner_rows():\n    return 1\n"
    c1 = repo.commit({"src/d.py": src})
    await _index(TRUNK, repo, c1, ["src/d.py"])
    await _remember(MEM, "dinner", "alice likes ramen for dinner", agent="other")
    await _remember(MEM, "dinner", "`fetch_dinner_rows` returns one")
    await _pass()
    c2 = repo.commit({"src/d.py": src.replace("return 1", "return 2")})
    await _index(TRUNK, repo, c2, ["src/d.py"])
    await _pass()
    found = await handle_memory_search(
        MemorySearchInput(org=ORG, namespace=MEM, agent="*", query="fetch_dinner_rows")
    )
    states = {
        "ramen" in h.content_excerpt: h.code_state
        for h in found.results
        if h.memory_key == "dinner"
    }
    assert states == {False: "changed", True: ""}


async def test_existing_memories_are_backfilled_on_the_first_pass(env, ch_client) -> None:
    from server.code.app import memory_links
    from server.memory.workers.code_links import run_pass

    repo = env
    c1 = repo.commit({"src/old.py": "def legacy_value():\n    return 8\n"})
    await _index(TRUNK, repo, c1, ["src/old.py"])
    mid = await _remember(MEM, "legacy", "`legacy_value` is eight")
    ch_client.command("TRUNCATE TABLE loom.memory_code_link_queue")  # written before the queue
    await memory_links.set_link_cursor("queue", "")
    await memory_links.set_link_cursor("backfill", "")
    for _ in range(50):
        await run_pass()
        if await memory_links.link_cursor("backfill") == _BACKFILL_DONE:
            break
    assert [link["symbol"].rsplit("::", 1)[-1] for link in await _links(mid)] == ["legacy_value"]


async def test_a_name_left_in_a_comment_is_not_the_definition(env) -> None:
    repo = env
    src = "def fetch_rows(n):\n    return n\n\n\ndef other():\n    return 0\n"
    c1 = repo.commit({"src/c.py": src})
    await _index(TRUNK, repo, c1, ["src/c.py"])
    mid = await _remember(MEM, "fetch-rows", "`fetch_rows` returns n")
    await _pass()
    left = "# fetch_rows was removed; call fetch_rows_v2\ndef other():\n    return 0\n"
    c2 = repo.commit({"src/c.py": left})
    await _index(TRUNK, repo, c2, ["src/c.py"])
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["missing"]


async def test_removing_a_method_leaves_its_namesake_alone(env) -> None:
    repo = env
    src = (
        "class Writer:\n    def read_rows(self):\n        return 1\n\n\n"
        "class Reader:\n    def read_rows(self):\n        return 2\n"
    )
    c1 = repo.commit({"src/rw.py": src})
    await _index(TRUNK, repo, c1, ["src/rw.py"])
    mid = await _remember(MEM, "writer-rows", "`Writer.read_rows` returns one")
    await _pass()
    c2 = repo.commit({"src/rw.py": "class Reader:\n    def read_rows(self):\n        return 2\n"})
    await _index(TRUNK, repo, c2, ["src/rw.py"])
    await _pass()
    (link,) = await _links(mid)
    assert (link["symbol"].rsplit("::", 2)[-2:], link["state"]) == (
        ["Writer", "read_rows"],
        "missing",
    )
    assert await _lessons(TRUNK, "Reader.read_rows") == []


async def test_the_fingerprint_is_the_heads_not_an_unpublished_commits(env) -> None:
    from server.code.app.index.ingest import ingest_file
    from server.code.store.index import CodeRepo
    from server.embedding.fake import FakeNGramEmbedder
    from server.settings import settings

    repo = env
    src = "def tally(xs):\n    return sum(xs)\n"
    c1 = repo.commit({"src/t.py": src})
    await _index(TRUNK, repo, c1, ["src/t.py"])
    edited = src.replace("sum(xs)", "len(xs)")
    c2 = repo.commit({"src/t.py": edited})
    # c2's rows are being ingested; the head is still c1
    await ingest_file(
        CodeRepo(),
        ORG,
        TRUNK,
        path="src/t.py",
        source=edited.encode(),
        commit_sha=c2,
        embedding_model="fake-ngram-1024",
        embedder=FakeNGramEmbedder(dim=settings.embedding_dim),
    )
    mid = await _remember(MEM, "tally", "`tally` sums its input")
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["fresh"]
    await CodeRepo().set_state(ORG, TRUNK, "git_head", c2)
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["changed"]


async def test_a_worker_stopped_past_the_queue_ttl_backfills_an_empty_queue(env, ch_client) -> None:
    import json

    from server.code.app import memory_links
    from server.memory.store import code_links as store
    from server.memory.workers.code_links import run_pass

    repo = env
    c1 = repo.commit({"src/q.py": "def q_one():\n    return 1\n\n\ndef q_two():\n    return 2\n"})
    await _index(TRUNK, repo, c1, ["src/q.py"])
    mid = await _remember(MEM, "q", "`q_one` is one")
    await _pass()
    await _remember(MEM, "q", "`q_two` is two")  # an edit the stopped worker never read
    ch_client.command("TRUNCATE TABLE loom.memory_code_link_queue")  # aged out
    horizon = await store.queue_horizon()
    old = [horizon - 10**6, 0, "", "", "", "", "", ""]
    await memory_links.set_link_cursor("queue", json.dumps(old))
    await memory_links.set_link_cursor("backfill", _BACKFILL_DONE)
    for _ in range(50):
        await run_pass()
        if await memory_links.link_cursor("backfill") == _BACKFILL_DONE:
            break
    assert [link["symbol"].rsplit("::", 1)[-1] for link in await _links(mid)] == ["q_two"]
    assert json.loads(await memory_links.link_cursor("queue"))[0] > horizon


async def test_a_pass_behind_the_queue_ttl_that_fails_still_owes_its_backfill(
    env, ch_client, monkeypatch
) -> None:
    import json

    from server.code.app import memory_links
    from server.memory.app import code_links
    from server.memory.store import code_links as store
    from server.memory.workers.code_links import run_pass

    repo = env
    c1 = repo.commit({"src/q.py": "def q_one():\n    return 1\n\n\ndef q_two():\n    return 2\n"})
    await _index(TRUNK, repo, c1, ["src/q.py"])
    mid = await _remember(MEM, "q", "`q_one` is one")
    await _pass()
    await _remember(MEM, "q", "`q_two` is two")  # an edit the stopped worker never read
    ch_client.command("TRUNCATE TABLE loom.memory_code_link_queue")  # aged out
    horizon = await store.queue_horizon()
    await memory_links.set_link_cursor("queue", json.dumps([horizon - 10**6, 0, *[""] * 6]))
    await memory_links.set_link_cursor("backfill", _BACKFILL_DONE)
    # no repository is new: only the queue's TTL owes the backfill
    configured = memory_links.configured_code_namespaces()
    await memory_links.set_link_cursor("repos", json.dumps(configured))

    async def down(*args: Any) -> Any:
        raise RuntimeError("backfill read failed")

    with monkeypatch.context() as m:
        m.setattr(code_links, "backfill", down)
        # the queue cursor moves on, then the backfill fails: an error, the recheck still runs
        assert (await run_pass())["errors"] == 1
    assert json.loads(await memory_links.link_cursor("queue"))[0] > horizon
    for _ in range(50):
        await run_pass()
        if await memory_links.link_cursor("backfill") == _BACKFILL_DONE:
            break
    assert [link["symbol"].rsplit("::", 1)[-1] for link in await _links(mid)] == ["q_two"]


async def test_a_backfill_in_progress_behind_the_queue_ttl_starts_over(env, ch_client) -> None:
    import json

    from server.code.app import memory_links
    from server.memory.store import code_links as store
    from server.memory.workers.code_links import run_pass

    c1 = env.commit({"src/q.py": "def q_one():\n    return 1\n\n\ndef q_two():\n    return 2\n"})
    await _index(TRUNK, env, c1, ["src/q.py"])
    mid = await _remember(MEM, "q", "`q_one` is one")
    await _pass()
    (ident,) = ch_client.query(
        "SELECT org, namespace, agent, user_id, session_id, memory_key FROM loom.memory FINAL "
        "WHERE memory_id = {m:UUID}",
        parameters={"m": mid},
    ).result_rows
    await _remember(MEM, "q", "`q_two` is two")  # written while the worker was stopped
    ch_client.command("TRUNCATE TABLE loom.memory_code_link_queue")  # aged out
    horizon = await store.queue_horizon()
    await memory_links.set_link_cursor("queue", json.dumps([horizon - 10**6, 0, *[""] * 6]))
    # a scan in progress, already past this memory
    past = {"after": [*ident[:5], ident[5] + "~"]}
    await memory_links.set_link_cursor("backfill", json.dumps(past))
    configured = memory_links.configured_code_namespaces()
    await memory_links.set_link_cursor("repos", json.dumps(configured))
    for _ in range(50):
        await run_pass()
        if await memory_links.link_cursor("backfill") == _BACKFILL_DONE:
            break
    assert [x["symbol"].rsplit("::", 1)[-1] for x in await _links(mid)] == ["q_two"]


async def test_the_backfill_drops_links_of_a_memory_that_no_longer_names_a_repository(
    env, ch_client
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import code_link_pass, code_links
    from server.memory.app.write import handle_memory_set

    repo = env
    c1 = repo.commit({"src/u.py": "def untag_me():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/u.py"])
    mid = await _remember(MEM, "untag", "`untag_me` is one")
    await _pass()
    assert await _links(mid)
    await handle_memory_set(
        MemorySetInput(
            org=ORG,
            namespace=MEM,
            agent="claude-code",
            memory_key="untag",
            value="`untag_me` is one",
            tags=[],
        )
    )
    ch_client.command("TRUNCATE TABLE loom.memory_code_link_queue")  # the edit aged out
    ch_client.command("OPTIMIZE TABLE loom.memory FINAL")  # and the tagged version merged away
    cursor: tuple[str, ...] | None = None
    while True:
        cursor = await code_links.backfill(cursor, 500, code_link_pass.PassResult())
        if cursor is None:
            break
    assert await _links(mid) == []


async def test_validate_counts_only_repositories_the_caller_may_read(env) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models_namespace_catalog import NamespaceValidateInput
    from server.identity.app.namespace_validate import handle_namespace_validate
    from server.operations.base import Role

    repo = env
    src = "def secret_value():\n    return 1\n"
    c1 = repo.commit({"src/s.py": src})
    await _index(TRUNK, repo, c1, ["src/s.py"])
    await _remember(MEM, "secret", "`secret_value` is one")
    await _pass()
    c2 = repo.commit({"src/s.py": src.replace("1", "2")})
    await _index(TRUNK, repo, c2, ["src/s.py"])
    await _pass()

    async def stale_for(namespaces: tuple[str, ...]) -> Any:
        set_current_auth(
            AuthInfo(
                subject="it@example.com",
                org=ORG,
                namespaces=namespaces,
                roles=(Role.VIEWER,),
                is_service_account=False,
            )
        )
        try:
            report = await handle_namespace_validate(NamespaceValidateInput(org=ORG, namespace=MEM))
        finally:
            set_current_auth(None)
        return next(f for f in report.findings if f.check == "code_links_stale")

    assert (await stale_for((MEM, TRUNK))).count == 1
    memory_only = await stale_for((MEM,))
    assert (memory_only.count, memory_only.samples) == (0, [])


async def test_stale_links_count_and_sample_each_memory_once(env) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.app.code_link_reads import stale_links
    from server.operations.base import Role

    repo = env
    src = "def alpha_val():\n    return 1\n\n\ndef beta_val():\n    return 2\n"
    c1 = repo.commit({"src/ab.py": src})
    await _index(TRUNK, repo, c1, ["src/ab.py"])
    # two users' memories under one key, one of them linked twice
    await _remember(MEM, "ab", "`alpha_val` and `beta_val` return one and two", user_id="ann")
    await _remember(MEM, "ab", "`alpha_val` returns one", user_id="bob")
    await _pass()
    c2 = repo.commit({"src/ab.py": src.replace("1", "3").replace("2", "4")})
    await _index(TRUNK, repo, c2, ["src/ab.py"])
    await _pass()
    set_current_auth(
        AuthInfo(
            subject="it@example.com",
            org=ORG,
            namespaces=(MEM, TRUNK),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    try:
        links = await stale_links(ORG, MEM, limit=10)
    finally:
        set_current_auth(None)
    assert [(a["memory_key"], a["state"], a["total"]) for a in links] == [
        ("ab", "changed", 2),
        ("ab", "changed", 2),
    ]


async def test_loom_writes_no_memory_version(env, ch_client) -> None:
    repo = env
    c1 = repo.commit({"src/w.py": "def write_free():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/w.py"])
    await _remember(MEM, "write-free", "`write_free` is one")
    await _pass()
    c2 = repo.commit({"src/w.py": None})
    await _index(TRUNK, repo, c2, ["src/w.py"])
    await _pass()
    actors = ch_client.query(
        "SELECT DISTINCT actor FROM loom.memory_versions WHERE org = {org:String} "
        "AND namespace = {ns:String} AND memory_key = 'write-free'",
        parameters={"org": ORG, "ns": MEM},
    ).result_rows
    assert all("code-links" not in str(a[0]) for a in actors), actors


async def _drop_fetch_rows(repo: _Repo, *namespaces: str) -> None:
    src = "def fetch_rows(n):\n    return n\n\n\ndef other():\n    return 0\n"
    c1 = repo.commit({"src/c.py": src})
    for ns in namespaces:
        await _index(ns, repo, c1, ["src/c.py"])


async def _remove_fetch_rows(repo: _Repo, ns: str) -> None:
    c2 = repo.commit({"src/c.py": "def other():\n    return 0\n"})
    await _index(ns, repo, c2, ["src/c.py"])


async def test_a_memory_rewritten_about_something_else_is_served_before_it_relinks(env) -> None:
    from server.settings import settings

    await _drop_fetch_rows(env, TRUNK)
    await _remember(MEM, "note", "`fetch_rows` returns n")
    await _pass()
    await _remove_fetch_rows(env, TRUNK)
    await _pass()
    assert "note" not in await _search(MEM, "fetch_rows returns n")
    settings_lag = settings.memory_code_links_settle_seconds
    await _remember(MEM, "note", "the team prefers vegetarian lunches")
    # not relinked yet: the links describe the old value and say nothing about this one
    assert settings_lag == 0 and (await _search(MEM, "vegetarian lunches")).get("note") == ""


async def test_a_pending_name_keeps_a_memory_whose_other_code_is_gone(env) -> None:
    await _drop_fetch_rows(env, TRUNK)
    await _remember(MEM, "swap", "`fetch_rows` is replaced by `Planner.plan_route`")
    await _pass()
    await _remove_fetch_rows(env, TRUNK)
    await _pass()
    assert (await _search(MEM, "fetch_rows replaced by plan_route")).get("swap") == "missing"


async def test_a_hidden_memory_gives_its_slot_and_the_verdict_to_the_next(env) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search

    await _drop_fetch_rows(env, TRUNK)
    await _remember(MEM, "gone", "`fetch_rows` returns the widget rows")
    await _remember(MEM, "kept", "widget rows come back in pages")
    await _pass()
    query = "`fetch_rows` returns the widget rows"

    async def top1() -> Any:
        return await handle_memory_search(
            MemorySearchInput(org=ORG, namespace=MEM, agent="*", query=query, top_k=1)
        )

    assert [h.memory_key for h in (await top1()).results] == ["gone"]
    await _remove_fetch_rows(env, TRUNK)
    await _pass()
    out = await top1()
    assert [h.memory_key for h in out.results] == ["kept"] and not out.abstain


async def test_code_gone_from_trunk_still_teaches_the_branch_that_has_it(env) -> None:
    await _drop_fetch_rows(env, TRUNK, BRANCH)
    await _remember(MEM, "rows", "`fetch_rows` returns n unchanged")
    await _pass()
    await _remove_fetch_rows(env, TRUNK)
    await _pass()
    assert await _lessons(TRUNK, "fetch_rows") == []
    (lesson,) = await _lessons(BRANCH, "fetch_rows")
    assert (lesson["memory_key"], lesson["code_state"]) == ("rows", "fresh")


async def test_moving_a_statement_out_of_a_block_reads_changed(env) -> None:
    repo = env
    inside = "def pick(x):\n    if x:\n        return 1\n        return 2\n    return 0\n"
    c1 = repo.commit({"src/p.py": inside})
    await _index(TRUNK, repo, c1, ["src/p.py"])
    mid = await _remember(MEM, "pick", "`pick` returns 1 for a truthy x")
    await _pass()
    outside = "def pick(x):\n    if x:\n        return 1\n    return 2\n    return 0\n"
    c2 = repo.commit({"src/p.py": outside})
    await _index(TRUNK, repo, c2, ["src/p.py"])
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["changed"]


async def _relink_once(repo: _Repo, path: str, after: str) -> str:
    c = repo.commit({path: after})
    await _index(TRUNK, repo, c, [path])
    await _pass()
    return c


async def test_a_decorator_or_template_head_is_part_of_the_code(env) -> None:
    repo = env
    py = "import functools\n\n\n@functools.lru_cache(maxsize=128)\ndef lookup(k):\n    return k\n"
    cpp = "namespace a {\ntemplate <int N = 1>\nint scaled() { return N; }\n}\n"
    c1 = repo.commit({"src/d.py": py, "src/t.cpp": cpp})
    await _index(TRUNK, repo, c1, ["src/d.py", "src/t.cpp"])
    py_mid = await _remember(MEM, "lookup", "`lookup` is cached")
    cpp_mid = await _remember(MEM, "scaled", "`a::scaled` returns one by default")
    await _pass()
    c2 = repo.commit(
        {"src/d.py": py.replace("128", "0"), "src/t.cpp": cpp.replace("N = 1", "N = 2")}
    )
    await _index(TRUNK, repo, c2, ["src/d.py", "src/t.cpp"])
    await _pass()
    assert [link["state"] for link in await _links(py_mid)] == ["changed"]
    assert [link["state"] for link in await _links(cpp_mid)] == ["changed"]


async def test_a_flipped_condition_is_changed_code(env, monkeypatch) -> None:
    from server.memory.app import code_link_judge

    repo = env
    cpp = "#if 1\nint picked() { return 3; }\n#else\nint picked() { return 4; }\n#endif\n"
    py = "if True:\n    def chosen():\n        return 3\nelse:\n    def chosen():\n        return 4\n"
    fast = "#ifdef FAST\nint quick() { return 3; }\n#endif\n"
    c1 = repo.commit({"src/p.cpp": cpp, "src/c.py": py, "src/q.cpp": fast})
    await _index(TRUNK, repo, c1, ["src/p.cpp", "src/c.py", "src/q.cpp"])
    await _set_policy(MEM, "revise")
    cpp_mid = await _remember(MEM, "picked", "`picked` returns three")
    py_mid = await _remember(MEM, "chosen", "`chosen` returns three")
    await _remember(MEM, "quick", "`quick` returns three")
    await _pass()
    seen: list[str] = []

    async def judge(claim: str, before: str, after: str) -> tuple[str, str]:
        seen.append(after)
        return "unsure", ""

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    flipped = {
        "src/p.cpp": cpp.replace("#if 1", "#if 0"),
        # renamed as well: the same tokens under another condition are no move
        "src/c.py": py.replace("if True", "if False").replace("chosen", "taken"),
        "src/q.cpp": fast.replace("#ifdef", "#ifndef"),
    }
    c2 = repo.commit(flipped)
    await _index(TRUNK, repo, c2, list(flipped))
    await _pass()
    assert [link["state"] for link in await _links(cpp_mid)] == ["changed"]
    assert [link["state"] for link in await _links(py_mid)] == ["unresolved"]
    assert any(text.startswith("(selected when: #ifndef FAST)\n") for text in seen)


async def test_an_unreadable_mirror_is_retried_not_read_as_removed_code(env) -> None:
    from server.code.app import memory_links as code
    from server.memory.app import code_link_pass, code_link_recheck

    repo = env
    src = "def kept_rows():\n    return 1\n\n\ndef other():\n    return 0\n"
    c1 = repo.commit({"src/k.py": src})
    await _index(TRUNK, repo, c1, ["src/k.py"])
    mid = await _remember(MEM, "kept", "`kept_rows` returns one")
    await _pass()
    c2 = repo.commit({"src/k.py": src.replace("return 0", "return 9")})
    await _index(TRUNK, repo, c2, ["src/k.py"])
    git_dir = repo.root / ".git"
    git_dir.rename(repo.root / ".git-away")
    try:
        result = code_link_pass.PassResult()
        await code_link_recheck.recheck_all(result)
        assert result.errors  # reported, not read as a deletion
    finally:
        (repo.root / ".git-away").rename(git_dir)
    assert [link["state"] for link in await _links(mid)] == ["fresh"]
    assert await code.checked_head(ORG, TRUNK) == c1  # retried from the same head
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["fresh"]
    assert await code.checked_head(ORG, TRUNK) == c2


async def test_a_parser_failure_is_retried_not_read_as_removed_code(env, monkeypatch) -> None:
    from server.code.app import memory_links as code
    from server.code.app.index import ingest_parse
    from server.memory.app import code_link_pass, code_link_recheck

    repo = env
    src = "def kept_rows():\n    return 1\n"
    c1 = repo.commit({"src/k.py": src})
    await _index(TRUNK, repo, c1, ["src/k.py"])
    mid = await _remember(MEM, "kept", "`kept_rows` returns one")
    await _pass()
    c2 = repo.commit({"src/k.py": src.replace("return 1", "return 9")})
    await _index(TRUNK, repo, c2, ["src/k.py"])

    def broken(path: str, source: bytes) -> None:
        raise RuntimeError("no python grammar")

    with monkeypatch.context() as m:
        m.setitem(ingest_parse._PARSERS, "python", broken)
        result = code_link_pass.PassResult()
        await code_link_recheck.recheck_all(result)
    assert result.errors  # reported, not read as a deletion
    assert [link["state"] for link in await _links(mid)] == ["fresh"]
    assert await code.checked_head(ORG, TRUNK) == c2  # the repository moves on
    await _pass()  # the parser is back: the file is rechecked from its own commit
    assert [link["state"] for link in await _links(mid)] == ["changed"]


async def test_a_file_that_never_parses_holds_back_no_other_file(env, monkeypatch) -> None:
    from server.code.app.index import ingest_parse
    from server.memory.app import code_link_pass, code_link_recheck

    repo = env
    c1 = repo.commit(
        {"src/a.py": "def alpha():\n    return 1\n", "src/b.py": "def beta():\n    return 1\n"}
    )
    await _index(TRUNK, repo, c1, ["src/a.py", "src/b.py"])
    alpha = await _remember(MEM, "alpha", "`alpha` returns one")
    beta = await _remember(MEM, "beta", "`beta` returns one")
    await _pass()
    c2 = repo.commit(
        {"src/a.py": "def alpha():\n    return 2\n", "src/b.py": "def beta():\n    return 2\n"}
    )
    await _index(TRUNK, repo, c2, ["src/a.py", "src/b.py"])
    parse = ingest_parse._PARSERS["python"]

    def b_never_parses(path: str, source: bytes) -> Any:
        if path.endswith("b.py"):
            raise RecursionError("maximum recursion depth exceeded")
        return parse(path, source)

    monkeypatch.setitem(ingest_parse._PARSERS, "python", b_never_parses)
    for _ in range(2):
        result = code_link_pass.PassResult()
        await code_link_recheck.recheck_all(result)
        assert any("src/b.py" in e for e in result.errors)
    assert [x["state"] for x in await _links(alpha)] == ["changed"]
    assert [x["state"] for x in await _links(beta)] == ["fresh"]  # as last checked


async def test_a_host_that_does_not_know_the_repository_keeps_its_links(env) -> None:
    from server.code.domain.index.repos import configured_repos, set_registry_repos

    c1 = env.commit({"src/f.py": "def fetch_rows():\n    return 1\n"})
    await _index(TRUNK, env, c1, ["src/f.py"])
    mid = await _remember(MEM, "rows", "`fetch_rows` returns one")
    await _pass()
    await _relink_once(env, "src/f.py", "def fetch_rows():\n    return 2\n")
    assert [x["state"] for x in await _links(mid)] == ["changed"]
    entries = [c for c in configured_repos() if c.source == "registry"]
    set_registry_repos([])  # a registry not loaded yet, or one that failed to refresh
    try:
        await _remember(MEM, "rows", "`fetch_rows` returns one", pinned=True)
        await _pass()
    finally:
        set_registry_repos(entries)
    await _pass()
    assert [x["state"] for x in await _links(mid)] == ["changed"]


async def test_a_name_two_files_define_never_links_to_the_one_left(env) -> None:
    from server.memory.app import code_link_pass, code_link_recheck
    from server.memory.store import code_links as store

    reader = "class Reader:\n    def close(self):\n        return {}\n"
    paths = ["src/a/reader.py", "src/b/reader.py"]
    c1 = env.commit({paths[0]: reader.format(1), paths[1]: reader.format(2)})
    await _index(TRUNK, env, c1, paths)
    mid = await _remember(MEM, "close", "`Reader.close` must flush the b buffer")
    await _pass()
    assert await store.for_memories(ORG, [mid]) == []  # neither linked nor waiting
    c2 = env.commit({paths[1]: None})
    await _index(TRUNK, env, c2, paths[1:])
    await code_link_recheck.recheck_all(code_link_pass.PassResult())  # the commit, no new write
    assert await store.for_memories(ORG, [mid]) == []


def _python_parser_fails_for(monkeypatch, suffix: str) -> Any:
    """Make the Python parser fail on paths ending in `suffix`; returns the undo."""
    from server.code.app.index import ingest_parse

    parse = ingest_parse._PARSERS["python"]

    def failing(path: str, source: bytes) -> Any:
        if path.endswith(suffix):
            raise RecursionError("maximum recursion depth exceeded")
        return parse(path, source)

    monkeypatch.setitem(ingest_parse._PARSERS, "python", failing)
    return lambda: monkeypatch.setitem(ingest_parse._PARSERS, "python", parse)


async def test_a_rehash_the_parser_failed_stays_due(env, ch_client, monkeypatch) -> None:
    from server.code.domain.declaration import fingerprint_version
    from server.memory.app import code_link_pass, code_link_recheck
    from server.memory.store import code_links as store

    c1 = env.commit({"src/k.py": "def kept_rows():\n    return 1\n"})
    await _index(TRUNK, env, c1, ["src/k.py"])
    mid = await _remember(MEM, "kept", "`kept_rows` returns one")
    await _pass()
    _as_old_rules(ch_client, body_hash="old", checked_hash="old", anchored_hash="old")
    restore = _python_parser_fails_for(monkeypatch, "k.py")
    await code_link_recheck.recheck_all(code_link_pass.PassResult())
    ((row,),) = [await store.for_memories(ORG, [mid])]
    assert (row["fingerprint_version"], row["body_hash"]) == ("old", "old")  # still due
    restore()
    await _pass()
    ((row,),) = [await store.for_memories(ORG, [mid])]
    assert row["fingerprint_version"] == fingerprint_version()
    assert row["checked_hash"] not in ("", "old") and row["state"] == "fresh"


async def test_a_path_that_did_not_parse_is_owed_before_its_batch_is_done(env, monkeypatch) -> None:
    from server.code.app import memory_links as code
    from server.memory.app import code_link_pass, code_link_recheck

    files = {"src/a.py": "def alpha():\n    return 1\n", "src/b.py": "def beta():\n    return 1\n"}
    c1 = env.commit(files)
    await _index(TRUNK, env, c1, list(files))
    beta = await _remember(MEM, "beta", "`beta` returns one")
    await _pass()
    c2 = env.commit({p: t.replace("return 1", "return 2") for p, t in files.items()})
    await _index(TRUNK, env, c2, list(files))
    monkeypatch.setattr(code_link_recheck, "_PATHS_PER_WRITE", 1)
    restore = _python_parser_fails_for(monkeypatch, "b.py")
    write = code.set_link_cursor

    async def cut_after_b(name: str, value: str) -> None:
        await write(name, value)
        if name.startswith("recheck/") and '"after": "src/b.py"' in value:
            raise RuntimeError("the pass is cut here")

    with monkeypatch.context() as m:
        m.setattr(code, "set_link_cursor", cut_after_b)
        await code_link_recheck.recheck_all(code_link_pass.PassResult())
    await code_link_recheck.recheck_all(code_link_pass.PassResult())  # finishes the head
    restore()
    await _pass()
    assert [x["state"] for x in await _links(beta)] == ["changed"]


async def test_a_memory_whose_file_does_not_parse_holds_back_no_other(env, monkeypatch) -> None:
    files = {"src/a.py": "def alpha():\n    return 1\n", "src/b.py": "def beta():\n    return 1\n"}
    c1 = env.commit(files)
    await _index(TRUNK, env, c1, list(files))
    restore = _python_parser_fails_for(monkeypatch, "b.py")
    alpha = await _remember(MEM, "alpha", "`alpha` returns one", files=["src/a.py"])
    beta = await _remember(MEM, "beta", "`beta` returns one", files=["src/b.py"])
    from server.memory.app import code_link_pass, code_link_recheck, code_links

    for _ in range(2):  # the first link, then the retry of the one left waiting
        result = code_link_pass.PassResult()
        cursor = QUEUE_START
        cursor = await code_links.link_queued(cursor, 10_000, result)
        await code_link_recheck.recheck_all(result)
    assert [x["state"] for x in await _links(alpha)] == ["fresh"]
    assert await _links(beta) == []
    restore()
    await code_link_recheck.recheck_all(code_link_pass.PassResult())  # its marker is relinked
    assert [x["state"] for x in await _links(beta)] == ["fresh"]


async def test_a_late_link_of_an_older_value_is_relinked_to_the_newest(env) -> None:
    from server.memory.api.models_crud import MemoryGetInput
    from server.memory.app import code_link_pass, code_links
    from server.memory.app.handlers import handle_memory_get
    from server.memory.store import code_links as store

    c1 = env.commit({"src/k.py": "def fetch_rows():\n    return 1\n"})
    await _index(TRUNK, env, c1, ["src/k.py"])
    mid = await _remember(MEM, "late", "`fetch_rows` returns one")
    await _pass()
    (older,) = await store.memory_heads(ORG, [mid])
    assert await _remember(MEM, "late", "`fetch_rows` returns one, always") == mid
    await _pass()
    # a worker that read the older value before the rewrite writes its links last
    await code_links.link_memories([older], code_link_pass.PassResult())
    got = await handle_memory_get(
        MemoryGetInput(org=ORG, namespace=MEM, agent="claude-code", memory_key="late")
    )
    assert [c.symbol.rsplit("::", 1)[-1] for c in got.code] == ["fetch_rows"]


async def test_a_rename_after_an_edit_still_reads_changed(env) -> None:
    repo = env
    c1 = repo.commit({"src/r.py": "def fetch_rows():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/r.py"])
    mid = await _remember(MEM, "rows", "`fetch_rows` returns one")
    await _pass()
    await _relink_once(repo, "src/r.py", "def fetch_rows():\n    return 2\n")
    assert [link["state"] for link in await _links(mid)] == ["changed"]
    await _relink_once(repo, "src/r.py", "def load_rows():\n    return 2\n")
    (link,) = await _links(mid)
    assert (link["symbol"].rsplit("::", 1)[-1], link["state"]) == ("load_rows", "changed")


async def test_a_rename_that_changes_a_literal_is_a_change(env) -> None:
    repo = env
    c1 = repo.commit({"src/l.py": 'def fetch_rows():\n    return "fetch_rows"\n'})
    await _index(TRUNK, repo, c1, ["src/l.py"])
    mid = await _remember(MEM, "name", "`fetch_rows` returns its own name")
    await _pass()
    await _relink_once(repo, "src/l.py", 'def load_rows():\n    return "load_rows"\n')
    # renamed, and what it returns changed: the claim needs a look
    assert [link["state"] for link in await _links(mid)] == ["changed"]
    assert (await _search(MEM, "fetch_rows returns its own name")).get("name") == "changed"


async def test_another_edit_to_changed_code_is_judged_again(env, monkeypatch) -> None:
    from server.memory.app import code_link_judge

    repo = env
    c1 = repo.commit({"src/v.py": "def value():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/v.py"])
    await _set_policy(MEM, "revise")
    # the shared repository links earlier tests' memories too: note this one's judgements
    claim = f"`value` returns one ({MEM})"
    mid = await _remember(MEM, "value", claim)
    await _pass()
    seen: list[str] = []

    async def judge(judged: str, before: str, after: str) -> tuple[str, str]:
        returned = after.strip().rsplit(" ", 1)[-1]
        if judged == claim:
            seen.append(returned)
        return "contradicted", f"`value` returns {returned}"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _relink_once(repo, "src/v.py", "def value():\n    return 2\n")
    await _relink_once(repo, "src/v.py", "def value():\n    return 3\n")
    assert seen == ["2", "3"]
    (link,) = await _links(mid)
    assert (link["state"], link["suggestion"]) == ("changed", "`value` returns 3")


async def test_new_fingerprint_rules_judge_a_correction_again(env, ch_client, monkeypatch) -> None:
    from server.memory.app import code_link_judge

    repo = env
    c1 = repo.commit({"src/v.py": "def value():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/v.py"])
    await _set_policy(MEM, "revise")
    # the shared repository links earlier tests' memories too: count this one's judgements
    claim = f"`value` returns one ({MEM})"
    mid = await _remember(MEM, "value", claim)
    await _pass()
    calls: list[str] = []

    async def judge(judged: str, before: str, after: str) -> tuple[str, str]:
        calls.append(judged)
        return "contradicted", f"`value` returns {after.split()[-1]}"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _relink_once(repo, "src/v.py", "def value():\n    return 2\n")
    assert [x["suggestion"] for x in await _links(mid)] == ["`value` returns 2"]
    # the link as the previous fingerprint rules left it: every hash in their terms, so
    # equal hashes there cannot show the correction still holds
    _as_old_rules(ch_client, body_hash="old-1", checked_hash="old-2", judged_hash="old-2")
    await _pass()
    await _pass()
    assert [x["suggestion"] for x in await _links(mid)] == ["`value` returns 2"]
    assert calls.count(claim) == 2  # judged once more, against the code as it is


def _as_old_rules(ch_client, **fields: str) -> None:
    """Every link of this test's memories as rules that hashed differently left them."""
    sets = ", ".join(f"{k} = '{v}'" for k, v in {"fingerprint_version": "old", **fields}.items())
    ch_client.command(
        f"ALTER TABLE loom.memory_code_links UPDATE {sets} "
        "WHERE org = {o:String} AND namespace = {n:String} SETTINGS mutations_sync = 2",
        parameters={"o": ORG, "n": MEM},
    )


async def test_unchanged_code_stays_fresh_through_new_fingerprint_rules(env, ch_client) -> None:
    repo = env
    src = "#if defined(READY)\nint ready() { return 1; }\n#endif\nint other() { return 0; }\n"
    c1 = repo.commit({"src/r.cpp": src})
    await _index(TRUNK, repo, c1, ["src/r.cpp"])
    mid = await _remember(MEM, "ready", "`ready` returns one")
    await _pass()
    _as_old_rules(ch_client, body_hash="old", checked_hash="old", anchored_hash="old")
    await _relink_once(repo, "src/r.cpp", src.replace("return 0", "return 5"))  # a neighbour
    assert [link["state"] for link in await _links(mid)] == ["fresh"]
    assert [lesson["code_state"] for lesson in await _lessons(TRUNK, "ready")] == ["fresh"]


async def test_a_change_the_old_rules_missed_is_judged_after_new_ones(
    env, ch_client, monkeypatch
) -> None:
    from server.memory.app import code_link_judge

    repo = env
    src = "if True:\n    def chosen():\n        return 3\nelse:\n    def chosen():\n        return 4\n"
    c1 = repo.commit({"src/c.py": src})
    await _index(TRUNK, repo, c1, ["src/c.py"])
    await _set_policy(MEM, "revise")
    claim = f"`chosen` returns three ({MEM})"
    mid = await _remember(MEM, "chosen", claim)
    await _pass()

    async def unsure(judged: str, before: str, after: str) -> tuple[str, str]:
        return "unsure", ""

    monkeypatch.setattr(code_link_judge, "judge_claim", unsure)
    await _relink_once(repo, "src/c.py", src.replace("if True", "if False"))
    # as the old rules left it: blind to the condition, so still fresh and due for nothing
    _as_old_rules(
        ch_client,
        state="fresh",
        changed_since="",
        body_hash="old",
        checked_hash="old",
        policy_due="0",
    )
    calls: list[str] = []

    async def judge(judged: str, before: str, after: str) -> tuple[str, str]:
        calls.append(judged)
        return "contradicted", "`chosen` returns four"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _pass()
    await _pass()
    (link,) = await _links(mid)
    assert (link["state"], link["suggestion"]) == ("changed", "`chosen` returns four")
    assert calls.count(claim) == 1


async def test_a_policy_row_never_supersedes_a_relink_written_after_its_read(env) -> None:
    from server.memory.store import code_links as store

    repo = env
    c1 = repo.commit(
        {"src/p.py": "def first_one():\n    return 1\n\n\ndef second_one():\n    return 2\n"}
    )
    await _index(TRUNK, repo, c1, ["src/p.py"])
    mid = await _remember(MEM, "p", "`first_one` returns one")
    await _pass()
    read = await store.for_memories(ORG, [mid])  # what a slow policy pass read
    mid2 = await _remember(MEM, "p", "`second_one` returns two")  # the queue relinks meanwhile
    await _pass()
    await store.insert([store.link_row(a, follows=int(a["version"]), policy_due=0) for a in read])
    assert [link["symbol"].rsplit("::", 1)[-1] for link in await _links(mid2)] == ["second_one"]


async def test_validate_ignores_links_made_for_an_earlier_value(env) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models_namespace_catalog import NamespaceValidateInput
    from server.identity.app.namespace_validate import handle_namespace_validate
    from server.operations.base import Role

    await _drop_fetch_rows(env, TRUNK)
    await _remember(MEM, "note", "`fetch_rows` returns n")
    await _pass()
    await _remove_fetch_rows(env, TRUNK)
    await _pass()

    async def stale() -> int:
        set_current_auth(
            AuthInfo(
                subject="it@example.com",
                org=ORG,
                namespaces=(MEM, TRUNK),
                roles=(Role.VIEWER,),
                is_service_account=False,
            )
        )
        try:
            report = await handle_namespace_validate(NamespaceValidateInput(org=ORG, namespace=MEM))
        finally:
            set_current_auth(None)
        return next(f for f in report.findings if f.check == "code_links_stale").count

    assert await stale() == 1
    await _remember(MEM, "note", "My lunch preference is vegetarian")
    assert await stale() == 0


async def test_a_short_qualified_method_is_linked(env) -> None:
    repo = env
    src = "class Reader:\n    def get(self):\n        return 1\n\n    def put(self):\n        return 2\n"
    c1 = repo.commit({"src/g.py": src})
    await _index(TRUNK, repo, c1, ["src/g.py"])
    mid = await _remember(MEM, "get", "`Reader.get` returns one")
    await _pass()
    (link,) = await _links(mid)
    assert link["symbol"].rsplit("::", 2)[-2:] == ["Reader", "get"]
    await _relink_once(repo, "src/g.py", "class Reader:\n    def put(self):\n        return 2\n")
    assert "get" not in await _search(MEM, "Reader.get returns one")


async def test_a_recheck_never_supersedes_a_relink_written_after_its_read(env) -> None:
    from dataclasses import replace

    from server.memory.app import code_link_pass, code_link_recheck
    from server.memory.store import code_links as store

    repo = env
    c1 = repo.commit(
        {"src/q.py": "def first_one():\n    return 1\n\n\ndef second_one():\n    return 2\n"}
    )
    await _index(TRUNK, repo, c1, ["src/q.py"])
    mid = await _remember(MEM, "q", "`first_one` returns one")
    await _pass()
    (read,) = await store.for_memories(ORG, [mid])  # what a paused recheck read
    # the memory is edited, still about the same function, and the queue relinks it
    await _remember(MEM, "q", "`first_one` returns one, always")
    await _pass()
    stale = code_link_recheck.Move(read, replace(code_link_pass.link_of(read), state="changed"))
    await code_link_recheck._write_moves([stale], c1)  # the recheck resumes
    assert [(x["symbol"].rsplit("::", 1)[-1], x["state"]) for x in await _links(mid)] == [
        ("first_one", "fresh")
    ]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            '@deco(")")\ndef wrapped():\n    return 1\n',
            '@deco("]")\ndef wrapped():\n    return 1\n',
        ),
        (
            "@cache\n# why\ndef wrapped():\n    return 1\n",
            "@other\n# why\ndef wrapped():\n    return 1\n",
        ),
        (
            "@deco(\n"
            + "".join(f"    a{i}=1,\n" for i in range(30))
            + ")\ndef wrapped():\n    return 1\n",
            "@deco(\n"
            + "".join(f"    a{i}=2,\n" for i in range(30))
            + ")\ndef wrapped():\n    return 1\n",
        ),
    ],
)
async def test_any_decorator_edit_reads_changed(env, before: str, after: str) -> None:
    repo = env
    c1 = repo.commit({"src/w.py": "def deco(*a, **k):\n    return lambda f: f\n\n\n" + before})
    await _index(TRUNK, repo, c1, ["src/w.py"])
    mid = await _remember(MEM, "wrapped", "`wrapped` returns one")
    await _pass()
    await _relink_once(repo, "src/w.py", "def deco(*a, **k):\n    return lambda f: f\n\n\n" + after)
    assert [link["state"] for link in await _links(mid)] == ["changed"]


@pytest.mark.parametrize(
    ("path", "before", "after", "state"),
    [
        # a neighbour on the line edited: the definition's own bytes are as they were
        (
            "src/pair.cpp",
            "int fetch_rows() { return 3; } int unrelated() { return 3; }\n",
            "int fetch_rows() { return 3; } int unrelated() { return 4; }\n",
            "fresh",
        ),
        (
            "src/pair2.cpp",
            "int unrelated() { return 3; } int fetch_rows() { return 3; }\n",
            "int unrelated() { return 4; } int fetch_rows() { return 3; }\n",
            "fresh",
        ),
        (
            "src/pair.js",
            "function fetch_rows() { return 3; } function unrelated() { return 3; }\n",
            "function fetch_rows() { return 3; } function unrelated() { return 4; }\n",
            "fresh",
        ),
        (
            "src/pair3.cpp",
            "int fetch_rows() { return 3; } int unrelated() { return 3; }\n",
            "int fetch_rows() { return 4; } int unrelated() { return 3; }\n",
            "changed",
        ),
        # a sibling binding of the same `const` edited
        (
            "src/bind.js",
            "export const fetch_rows = () => 3, unrelated = () => 3;\n",
            "export const fetch_rows = () => 3, unrelated = () => 4;\n",
            "fresh",
        ),
        (
            "src/bind.ts",
            "export const unrelated = () => 3, fetch_rows = () => 3;\n",
            "export const unrelated = () => 4, fetch_rows = () => 3;\n",
            "fresh",
        ),
        # a binding on a later line: its `const` is part of it
        (
            "src/later.ts",
            "export const unrelated = () => 3,\n  fetch_rows = () => 3;\n",
            "export let unrelated = () => 3,\n  fetch_rows = () => 3;\n",
            "changed",
        ),
        # a sibling's comment goes with the sibling
        (
            "src/note.js",
            "export const fetch_rows = () => 3,\n  unrelated = () => 3 /* unrelated returns 3 */;\n",
            "export const fetch_rows = () => 3,\n  unrelated = () => 4 /* unrelated returns 4 */;\n",
            "fresh",
        ),
        # overloads on one line cannot be told apart: the line decides
        (
            "src/pair4.cpp",
            "int fetch_rows(int) { return 1; } int fetch_rows(double) { return 3; }\n",
            "int fetch_rows(int) { return 1; } int fetch_rows(double) { return 4; }\n",
            "changed",
        ),
    ],
)
async def test_a_definition_is_fingerprinted_by_its_own_bytes(
    env, path: str, before: str, after: str, state: str
) -> None:
    repo = env
    c1 = repo.commit({path: before})
    await _index(TRUNK, repo, c1, [path])
    mid = await _remember(MEM, "rows", "`fetch_rows` returns three", files=[path])
    await _pass()
    await _relink_once(repo, path, after)
    assert [link["state"] for link in await _links(mid)] == [state]


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "src/raw.cpp",
            'const char* fetch_rows() { return R"(a " fetch_rows " b)"; }\n',
            'const char* load_rows() { return R"(a " load_rows " b)"; }\n',
        ),
        (
            "src/re.ts",
            'export function fetch_rows(): boolean { return /fetch_rows/.test("fetch_rows"); }\n',
            'export function load_rows(): boolean { return /load_rows/.test("fetch_rows"); }\n',
        ),
        (
            "src/member.py",
            "def fetch_rows(data):\n    return data.fetch_rows\n",
            "def load_rows(data):\n    return data.load_rows\n",
        ),
    ],
)
async def test_a_rename_that_changes_what_the_code_does_is_not_a_move(
    env, path, before, after
) -> None:
    repo = env
    c1 = repo.commit({path: before})
    await _index(TRUNK, repo, c1, [path])
    mid = await _remember(MEM, "rows", "`fetch_rows` returns its rows", files=[path])
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["fresh"]
    await _relink_once(repo, path, after)
    assert [link["state"] for link in await _links(mid)] == ["changed"]


async def test_a_hold_taken_after_a_suggestion_withholds_it(env, monkeypatch) -> None:
    from server.memory.api.models_lock import MemoryLockInput
    from server.memory.app import code_link_judge
    from server.memory.app.lock import handle_memory_lock

    repo = env
    c1 = repo.commit({"src/h.py": "def held_value():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/h.py"])
    await _set_policy(MEM, "revise")
    mid = await _remember(MEM, "held", "`held_value` returns one")
    await _pass()

    async def judge(claim: str, before: str, after: str) -> tuple[str, str]:
        return "contradicted", "`held_value` returns two"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _relink_once(repo, "src/h.py", "def held_value():\n    return 2\n")
    assert [link["suggestion"] for link in await _links(mid)] == ["`held_value` returns two"]
    await handle_memory_lock(
        MemoryLockInput(
            org=ORG,
            namespace=MEM,
            agent="claude-code",
            memory_key="held",
            mode="no_delete",
            reason="kept",
        )
    )
    (link,) = await _links(mid)
    assert (link["state"], link["suggestion"]) == ("changed", "")


async def test_validate_follows_the_memorys_repository_binding(env) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models_namespace_catalog import NamespaceValidateInput
    from server.identity.app.namespace_validate import handle_namespace_validate
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set
    from server.operations.base import Role

    await _drop_fetch_rows(env, TRUNK)
    await _remember(MEM, "note", "`fetch_rows` returns n")
    await _pass()
    await _remove_fetch_rows(env, TRUNK)
    await _pass()

    async def stale() -> int:
        set_current_auth(
            AuthInfo(
                subject="it@example.com",
                org=ORG,
                namespaces=(MEM, TRUNK),
                roles=(Role.VIEWER,),
                is_service_account=False,
            )
        )
        try:
            report = await handle_namespace_validate(NamespaceValidateInput(org=ORG, namespace=MEM))
        finally:
            set_current_auth(None)
        return next(f for f in report.findings if f.check == "code_links_stale").count

    assert await stale() == 1
    await handle_memory_set(  # same text, no `repo:` tag: no longer about this repository
        MemorySetInput(
            org=ORG,
            namespace=MEM,
            agent="claude-code",
            memory_key="note",
            value="`fetch_rows` returns n",
        )
    )
    assert await stale() == 0


async def test_a_branch_keeps_the_lesson_fresh_after_a_trunk_rename(env) -> None:
    repo = env
    src = "def fetch_rows(n):\n    return fetch_rows(n - 1) if n else 0\n"
    c1 = repo.commit({"src/b.py": src})
    for ns in (TRUNK, BRANCH):
        await _index(ns, repo, c1, ["src/b.py"])
    await _remember(MEM, "rows", "`fetch_rows` counts down to zero")
    await _pass()
    await _relink_once(repo, "src/b.py", src.replace("fetch_rows", "load_rows"))
    assert [(x["memory_key"], x["code_state"]) for x in await _lessons(TRUNK, "load_rows")] == [
        ("rows", "moved")
    ]
    assert [(x["memory_key"], x["code_state"]) for x in await _lessons(BRANCH, "fetch_rows")] == [
        ("rows", "fresh")
    ]


async def test_for_file_names_code_that_is_gone(env) -> None:
    from server.memory.api.models_for_file import MemoryForFileInput
    from server.memory.app.for_file import handle_memory_for_file

    await _drop_fetch_rows(env, TRUNK)
    await _set_policy(MEM, "flag")
    await _remember(MEM, "rule", "read `fetch_rows` before editing", files=["src/c.py"])
    await _pass()
    await _remove_fetch_rows(env, TRUNK)
    await _pass()
    out = await handle_memory_for_file(
        MemoryForFileInput(org=ORG, namespace=MEM, path="src/c.py", extra_namespaces=[TRUNK])
    )
    (hit,) = [h for h in out.hits if h.memory_key == "rule"]
    assert hit.code_state == "missing" and "the code it is about is gone" in out.rendered


class _EraseBeforeInsert:
    """The link store's client, running a complete namespace erasure right before the
    first link insert `when` accepts: a writer that read before an erasure and writes
    after it."""

    def __init__(self, real: Any, when: Any) -> None:
        self.real, self.when = real, when
        self.erased: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)

    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> Any:
        if self.erased is None and self.when(rows):
            from server.erasure.app.namespace_purge import erase_namespaces

            self.erased = await erase_namespaces(
                ORG, [MEM], requester="it@example.com", purge=True, external=False
            )
        return await self.real.insert(table, rows, **kw)


def _erase_before_insert(monkeypatch, when: Any) -> _EraseBeforeInsert:
    from server.core.ch import get_client
    from server.memory.store import code_links as store

    hook = _EraseBeforeInsert(get_client(), when)
    monkeypatch.setattr(store, "_default_client", lambda: hook)
    return hook


def _all_links(ch_client) -> list[tuple]:
    return ch_client.query(
        "SELECT qualified_name, suggestion FROM loom.memory_code_links "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": ORG, "ns": MEM},
    ).result_rows


async def test_a_link_written_after_an_erasure_is_dropped(env, monkeypatch, ch_client) -> None:
    from server.memory.app import code_link_judge

    repo = env
    c1 = repo.commit({"src/e.py": "def erased_value():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/e.py"])
    # the queue: linking a memory written before the erasure
    await _remember(MEM, "e", "`erased_value` returns one")
    with monkeypatch.context() as m:
        queue = _erase_before_insert(m, lambda rows: True)
        await _pass()
    assert queue.erased.status == "completed" and _all_links(ch_client) == []
    # the recheck: moving a link after the erasure
    await _remember(MEM, "e", "`erased_value` returns one")
    await _pass()
    assert _all_links(ch_client)
    with monkeypatch.context() as m:
        recheck = _erase_before_insert(m, lambda rows: True)
        await _relink_once(repo, "src/e.py", "def erased_value():\n    return 2\n")
    assert recheck.erased.status == "completed" and _all_links(ch_client) == []
    # the policy: a suggestion written after the erasure
    await _set_policy(MEM, "revise")
    await _remember(MEM, "e", "`erased_value` returns two")
    await _pass()

    async def judge(claim: str, before: str, after: str) -> tuple[str, str]:
        return "contradicted", "`erased_value` returns three"

    with monkeypatch.context() as m:
        m.setattr(code_link_judge, "judge_claim", judge)
        policy = _erase_before_insert(m, lambda rows: any(r["suggestion"] for r in rows))
        await _relink_once(repo, "src/e.py", "def erased_value():\n    return 3\n")
    assert policy.erased.status == "completed" and _all_links(ch_client) == []


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "src/deco.py",
            'def constant(v):\n    return lambda f: lambda: v\n\n\n@constant("fetch_rows")\n'
            "def fetch_rows():\n    return None\n",
            'def constant(v):\n    return lambda f: lambda: v\n\n\n@constant("load_rows")\n'
            "def load_rows():\n    return None\n",
        ),
        (
            "src/q.cpp",
            "int fetch_rows() { return other::fetch_rows(); }\n",
            "int load_rows() { return other::load_rows(); }\n",
        ),
    ],
)
async def test_a_rename_that_also_renames_what_it_calls_is_not_a_move(
    env, path, before, after
) -> None:
    repo = env
    other = "namespace other { int fetch_rows() { return 1; } int load_rows() { return 2; } }\n"
    c1 = repo.commit({path: before, "src/other.h": other})
    await _index(TRUNK, repo, c1, [path, "src/other.h"])
    mid = await _remember(MEM, "rows", "`fetch_rows` returns its own value", files=[path])
    await _pass()
    (link,) = await _links(mid)
    assert (link["symbol"].rsplit("::", 1)[-1], link["path"], link["state"]) == (
        "fetch_rows",
        path,
        "fresh",
    )
    await _relink_once(repo, path, after)
    assert [x["state"] for x in await _links(mid)] == ["changed"]


async def test_a_suggestion_is_served_only_for_the_code_it_was_judged_against(
    env, monkeypatch
) -> None:
    from server.memory.app import code_link_judge
    from server.settings import settings

    repo = env
    c1 = repo.commit({"src/s.py": "def judged():\n    return 1\n"})
    for ns in (TRUNK, BRANCH):
        await _index(ns, repo, c1, ["src/s.py"])
    await _set_policy(MEM, "revise")
    mid = await _remember(MEM, "judged", "`judged` returns one")
    await _pass()

    async def judge(claim: str, before: str, after: str) -> tuple[str, str]:
        return "contradicted", "`judged` returns two"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _relink_once(repo, "src/s.py", "def judged():\n    return 2\n")
    c3 = repo.commit({"src/s.py": "def judged():\n    return 3\n"})
    await _index(BRANCH, repo, c3, ["src/s.py"])  # the branch moved on to 3
    (lesson,) = await _lessons(BRANCH, "judged")
    assert (lesson["code_state"], lesson["suggestion"]) == ("changed", "")
    (lesson,) = await _lessons(TRUNK, "judged")
    assert lesson["suggestion"] == "`judged` returns two"
    monkeypatch.setattr(settings, "memory_code_links_revise_per_pass", 0)
    await _index(TRUNK, repo, c3, ["src/s.py"])  # trunk too, with no judgement left
    await _pass()
    (link,) = await _links(mid)
    assert (link["state"], link["suggestion"]) == ("changed", "")


async def test_a_branch_keeps_its_lesson_across_successive_trunk_renames(env) -> None:
    repo = env
    src = "def fetch_rows(n):\n    return fetch_rows(n - 1) if n else 0\n"
    c1 = repo.commit({"src/n.py": src})
    for ns in (TRUNK, BRANCH):
        await _index(ns, repo, c1, ["src/n.py"])
    await _remember(MEM, "rows", "`fetch_rows` counts down to zero")
    await _pass()
    await _relink_once(repo, "src/n.py", src.replace("fetch_rows", "load_rows"))
    await _relink_once(repo, "src/n.py", src.replace("fetch_rows", "retrieve_rows"))
    assert [x["code_state"] for x in await _lessons(TRUNK, "retrieve_rows")] == ["moved"]
    assert [(x["memory_key"], x["code_state"]) for x in await _lessons(BRANCH, "fetch_rows")] == [
        ("rows", "fresh")
    ]


async def test_a_branch_at_an_intermediate_rename_keeps_its_lesson(env) -> None:
    repo = env
    src = "def fetch_rows(n):\n    return fetch_rows(n - 1) if n else 0\n"
    c1 = repo.commit({"src/n.py": src})
    await _index(TRUNK, repo, c1, ["src/n.py"])
    await _remember(MEM, "rows", "`fetch_rows` counts down to zero")
    await _pass()
    c2 = await _relink_once(repo, "src/n.py", src.replace("fetch_rows", "load_rows"))
    await _index(BRANCH, repo, c2, ["src/n.py"])  # the release branch is cut here
    cut = [(x["memory_key"], x["code_state"]) for x in await _lessons(BRANCH, "load_rows")]
    assert cut == [("rows", "fresh")]
    for name in ("retrieve_rows", "get_rows"):
        await _relink_once(repo, "src/n.py", src.replace("fetch_rows", name))
    assert [x["code_state"] for x in await _lessons(TRUNK, "get_rows")] == ["moved"]
    # the branch did not change: its lesson reads as it did
    assert [(x["memory_key"], x["code_state"]) for x in await _lessons(BRANCH, "load_rows")] == cut


async def test_a_branch_at_an_intermediate_rename_stays_fresh_through_new_rules(
    env, ch_client
) -> None:
    repo = env
    src = "def fetch_rows(n):\n    return fetch_rows(n - 1) if n else 0\n"
    c1 = repo.commit({"src/n.py": src})
    await _index(TRUNK, repo, c1, ["src/n.py"])
    await _remember(MEM, "rows", "`fetch_rows` counts down to zero")
    await _pass()
    c2 = await _relink_once(repo, "src/n.py", src.replace("fetch_rows", "load_rows"))
    await _index(BRANCH, repo, c2, ["src/n.py"])
    for name in ("retrieve_rows", "get_rows"):
        await _relink_once(repo, "src/n.py", src.replace("fetch_rows", name))
    # every hash, the former baselines too, as rules that hashed differently left them
    _as_old_rules(ch_client, body_hash="old", checked_hash="old", anchored_hash="old")
    ch_client.command(
        "ALTER TABLE loom.memory_code_links UPDATE former = arrayMap(f -> (f.1, f.2, "
        "concat('old-', f.3), f.4, f.5, f.6), former) "
        "WHERE org = {o:String} AND namespace = {n:String} SETTINGS mutations_sync = 2",
        parameters={"o": ORG, "n": MEM},
    )
    await _pass()
    assert [(x["memory_key"], x["code_state"]) for x in await _lessons(BRANCH, "load_rows")] == [
        ("rows", "fresh")
    ]


async def test_a_baseline_a_rename_moved_survives_an_edit_and_new_rules(env, ch_client) -> None:
    repo = env
    src = "def fetch_rows(n):\n    return fetch_rows(n - 1) if n else 0\n"
    c1 = repo.commit({"src/n.py": src})
    await _index(TRUNK, repo, c1, ["src/n.py"])
    await _remember(MEM, "rows", "`fetch_rows` counts down to zero")
    await _pass()
    loaded = src.replace("fetch_rows", "load_rows")
    c2 = await _relink_once(repo, "src/n.py", loaded)
    await _index(BRANCH, repo, c2, ["src/n.py"])  # the release branch keeps load_rows
    edited = loaded.replace("else 0", "else -1")
    await _relink_once(repo, "src/n.py", edited)
    await _relink_once(repo, "src/n.py", edited.replace("load_rows", "get_rows"))
    cut = [(x["memory_key"], x["code_state"]) for x in await _lessons(BRANCH, "load_rows")]
    assert cut == [("rows", "fresh")]
    _as_old_rules(ch_client, body_hash="old", checked_hash="old", anchored_hash="old")
    ch_client.command(
        "ALTER TABLE loom.memory_code_links UPDATE former = arrayMap(f -> (f.1, f.2, "
        "concat('old-', f.3), f.4, f.5, f.6), former) "
        "WHERE org = {o:String} AND namespace = {n:String} SETTINGS mutations_sync = 2",
        parameters={"o": ORG, "n": MEM},
    )
    await _pass()
    assert [(x["memory_key"], x["code_state"]) for x in await _lessons(BRANCH, "load_rows")] == cut
    assert [x["code_state"] for x in await _lessons(TRUNK, "get_rows")] == ["changed"]


async def test_validate_reports_an_unreadable_staleness_check_as_skipped(env, monkeypatch) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models_namespace_catalog import NamespaceValidateInput
    from server.identity.app.namespace_validate import handle_namespace_validate
    from server.memory.app import code_link_reads
    from server.operations.base import Role

    await _drop_fetch_rows(env, TRUNK)
    await _remember(MEM, "note", "`fetch_rows` returns n")
    await _pass()
    await _remove_fetch_rows(env, TRUNK)
    await _pass()

    async def timeout(*args: Any, **kw: Any) -> list[Any]:
        raise TimeoutError("read timed out")

    monkeypatch.setattr(code_link_reads, "stale_links", timeout)
    set_current_auth(
        AuthInfo(
            subject="it@example.com",
            org=ORG,
            namespaces=(MEM, TRUNK),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    try:
        report = await handle_namespace_validate(NamespaceValidateInput(org=ORG, namespace=MEM))
    finally:
        set_current_auth(None)
    assert "code_links_stale: TimeoutError" in report.skipped
    assert not [f for f in report.findings if f.check == "code_links_stale"]


async def test_a_failed_cleanup_after_an_erasure_is_swept_by_the_next_passes(
    env, monkeypatch, ch_client
) -> None:
    from server.memory.workers.code_links import run_pass

    repo = env
    c1 = repo.commit({"src/f.py": "def first_link():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/f.py"])
    await _remember(MEM, "f", "`first_link` returns one")

    class _CleanupFailsOnce(_EraseBeforeInsert):
        failed = False

        async def query_all(self, sql: str, params: Any = None) -> Any:
            if self.erased is not None and not self.failed and "FROM loom.memory FINAL" in sql:
                self.failed = True
                raise RuntimeError("read failed")
            return await self.real.query_all(sql, params=params)

    from server.core.ch import get_client
    from server.memory.store import code_links as store

    with monkeypatch.context() as m:
        hook = _CleanupFailsOnce(get_client(), lambda rows: True)
        m.setattr(store, "_default_client", lambda: hook)
        with pytest.raises(RuntimeError):
            await _pass()  # the insert lands, its cleanup fails
    assert hook.erased.status == "completed" and _all_links(ch_client)
    from server.code.app import memory_links as code
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_code_links_batch", 100_000)  # one sweep page
    await code.set_link_cursor("sweep", "")
    await run_pass()
    await run_pass()
    assert _all_links(ch_client) == []


async def test_a_repository_configured_later_links_the_memories_read_before_it(
    env, monkeypatch
) -> None:
    from server.code.app import memory_links as code
    from server.code.domain.index.repos import configured_repos, set_registry_repos
    from server.memory.workers.code_links import run_pass
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_code_links_batch", 100_000)  # one backfill page
    entries = [c for c in configured_repos() if c.source == "registry"]
    set_registry_repos([])
    mid = await _remember(MEM, "early", "`fetch_rows` returns one")
    await run_pass()  # read before its repository is configured: nothing to link it to
    await code.set_link_cursor("backfill", _BACKFILL_DONE)
    await run_pass()
    assert await _links(mid) == []
    set_registry_repos(entries)
    c1 = env.commit({"src/k.py": "def fetch_rows():\n    return 1\n"})
    await _index(TRUNK, env, c1, ["src/k.py"])
    await run_pass()
    assert [(x["symbol"].rsplit("::", 1)[-1], x["state"]) for x in await _links(mid)] == [
        ("fetch_rows", "fresh")
    ]


async def test_a_pass_that_fails_after_a_repository_is_configured_still_backfills(
    env, monkeypatch
) -> None:
    from server.code.app import memory_links as code
    from server.code.domain.index.repos import configured_repos, set_registry_repos
    from server.memory.app import code_links
    from server.memory.workers.code_links import run_pass
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_code_links_batch", 100_000)  # one backfill page
    entries = [c for c in configured_repos() if c.source == "registry"]
    set_registry_repos([])
    mid = await _remember(MEM, "early", "`fetch_rows` returns one")
    await run_pass()
    await code.set_link_cursor("backfill", _BACKFILL_DONE)
    await code.set_link_cursor("repos", "[]")  # none seen: the next configured one is new
    await run_pass()
    set_registry_repos(entries)
    c1 = env.commit({"src/k.py": "def fetch_rows():\n    return 1\n"})
    await _index(TRUNK, env, c1, ["src/k.py"])

    async def down(*args: Any) -> Any:
        raise RuntimeError("queue read failed")

    with monkeypatch.context() as m:
        m.setattr(code_links, "link_queued", down)
        # the repository is recorded as seen, then the linking fails
        assert (await run_pass())["errors"] == 1
    await run_pass()
    assert [x["state"] for x in await _links(mid)] == ["fresh"]


async def test_another_org_configuring_the_same_namespace_name_backfills(env, monkeypatch) -> None:
    import dataclasses

    from server.code.app import memory_links as code
    from server.code.domain.index.repos import configured_repos, set_registry_repos
    from server.memory.workers.code_links import run_pass
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_code_links_batch", 100_000)  # one backfill page
    entries = [c for c in configured_repos() if c.source == "registry"]
    # another org's repository under the same namespace names
    set_registry_repos([dataclasses.replace(c, org=f"{ORG}-other") for c in entries])
    mid = await _remember(MEM, "early", "`fetch_rows` returns one")
    await run_pass()
    await code.set_link_cursor("backfill", _BACKFILL_DONE)
    await run_pass()
    assert await _links(mid) == []
    set_registry_repos(entries)
    c1 = env.commit({"src/k.py": "def fetch_rows():\n    return 1\n"})
    await _index(TRUNK, env, c1, ["src/k.py"])
    await run_pass()
    assert [x["state"] for x in await _links(mid)] == ["fresh"]


async def test_a_failed_cleanup_is_swept_on_a_later_cycle_however_late(
    env, monkeypatch, ch_client
) -> None:
    """No time window: the sweep cycles through every linked memory, so a link left
    behind days ago is dropped while the queue cursor is still valid."""
    from server.code.app import memory_links as code
    from server.memory.store import code_links as store
    from server.memory.workers.code_links import run_pass

    await _drop_fetch_rows(env, TRUNK)
    mid = await _remember(MEM, "late", "`fetch_rows` returns n")
    await _pass()
    # the memory is gone, its link stayed (a cleanup that failed long ago)
    ch_client.command(
        "INSERT INTO loom.memory SELECT * REPLACE (now() AS tombstoned_at, version + 1 AS version) "
        "FROM (SELECT * FROM loom.memory FINAL WHERE memory_id = {m:UUID})",
        parameters={"m": mid},
    )
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_code_links_batch", 100_000)  # one sweep page
    await code.set_link_cursor("sweep", "")
    assert await store.for_memories(ORG, [mid])
    for _ in range(2):
        await run_pass()
    assert await store.for_memories(ORG, [mid]) == []


@pytest.mark.parametrize(
    ("path", "before", "after", "state"),
    [
        (
            "src/imp.py",
            "def fetch_rows():\n    from backend import fetch_rows\n    return fetch_rows()\n",
            "def load_rows():\n    from backend import load_rows\n    return load_rows()\n",
            "changed",
        ),
        (
            "src/mem.py",
            "def fetch_rows(data, n):\n    data.fetch_rows = 1\n    return fetch_rows(data, n - 1) if n else 1\n",
            "def load_rows(data, n):\n    data.fetch_rows = 1\n    return load_rows(data, n - 1) if n else 1\n",
            "moved",
        ),
        # `using backend::fetch_rows;` binds the bare name: the call may mean it now
        (
            "src/using.cpp",
            "namespace DB {\nint fetch_rows(int n = 0) {\n    if (!n) return 4;\n"
            "    using backend::fetch_rows;\n    return fetch_rows(0);\n}\n}\n",
            "namespace DB {\nint load_rows(int n = 0) {\n    if (!n) return 4;\n"
            "    using backend::fetch_rows;\n    return load_rows(0);\n}\n}\n",
            "changed",
        ),
        (
            "src/qual.cpp",
            "namespace DB {\nint fetch_rows(int n = 0) {\n    if (!n) return backend::fetch_rows(0);\n"
            "    return fetch_rows(0);\n}\n}\n",
            "namespace DB {\nint load_rows(int n = 0) {\n    if (!n) return backend::fetch_rows(0);\n"
            "    return load_rows(0);\n}\n}\n",
            "moved",
        ),
        (
            "src/lib.rs",
            "fn fetch_rows(n: u32) -> u32 {\n    use crate::other::fetch_rows;\n"
            "    if n > 0 { return fetch_rows(n - 1); }\n    3\n}\n",
            "fn load_rows(n: u32) -> u32 {\n    use crate::other::fetch_rows;\n"
            "    if n > 0 { return load_rows(n - 1); }\n    3\n}\n",
            "changed",
        ),
        (
            "src/alias.rs",
            "fn fetch_rows(n: u32) -> u32 {\n    use crate::other::fetch_rows as other_rows;\n"
            "    if n > 0 { return fetch_rows(n - 1); }\n    3\n}\n",
            "fn load_rows(n: u32) -> u32 {\n    use crate::other::fetch_rows as other_rows;\n"
            "    if n > 0 { return load_rows(n - 1); }\n    3\n}\n",
            "moved",
        ),
        # functions bound to a variable
        (
            "src/arrow.ts",
            "const fetch_rows = (): number => 3;\n",
            "const load_rows = (): number => 3;\n",
            "moved",
        ),
        (
            "src/exported.ts",
            "export const fetch_rows = (): number => 3;\n",
            "export const load_rows = (): number => 3;\n",
            "moved",
        ),
        (
            "src/fn.ts",
            "const fetch_rows = function (): number { return 3; };\n",
            "const load_rows = function (): number { return 3; };\n",
            "moved",
        ),
        # two declarations on one line: the shape is the one the symbol names
        (
            "src/line.cpp",
            "int anchor() { return 1; } int fetch_rows() { return 3; }\n",
            "int anchor() { return 1; } int load_rows() { return 4; }\n",
            "changed",
        ),
        (
            "src/line.ts",
            "export function anchor(): number { return 1; } export function fetch_rows(): number { return 3; }\n",
            "export function anchor(): number { return 1; } export function load_rows(): number { return 4; }\n",
            "changed",
        ),
        # overloads on one line: neither shape can be told from the other
        (
            "src/overloads.cpp",
            "int fetch_rows(int n) { return 1; } int fetch_rows(double n) { return 3; }\n",
            "int load_rows(int n) { return 1; } int load_rows(double n) { return 4; }\n",
            "changed",
        ),
        # on their own lines each overload has its shape
        (
            "src/split.cpp",
            "int fetch_rows(int n) { return 1; }\nint fetch_rows(double n) { return 3; }\n",
            "int load_rows(int n) { return 1; }\nint load_rows(double n) { return 4; }\n",
            "changed",
        ),
        (
            "src/split_rename.cpp",
            "int fetch_rows(int n) { return 1; }\nint fetch_rows(double n) { return 3; }\n",
            "int load_rows(int n) { return 1; }\nint load_rows(double n) { return 3; }\n",
            "moved",
        ),
        (
            "src/line.go",
            "package src\n\nfunc anchor() int { return 1 }; func fetch_rows() int { return 3 }\n",
            "package src\n\nfunc anchor() int { return 1 }; func load_rows() int { return 4 }\n",
            "changed",
        ),
        # the same tokens nested another way: renamed, and what the code does changed
        (
            "src/nest.py",
            "def fetch_rows(flag):\n    if flag:\n        return 3\n    return 4\n",
            "def load_rows(flag):\n    if flag:\n        return 3\n        return 4\n",
            "changed",
        ),
        (
            "src/asi.ts",
            "export function fetch_rows(): number {\n  return 3;\n}\n",
            "export function load_rows(): number {\n  return\n  3;\n}\n",
            "changed",
        ),
        # renamed while a sibling binding of the same `const` changed
        (
            "src/sibling.js",
            "export const fetch_rows = () => 3, unrelated = () => 3;\n",
            "export const load_rows = () => 3, unrelated = () => 4;\n",
            "moved",
        ),
        (
            "src/sibling.ts",
            "export const unrelated = () => 3, fetch_rows = () => 3;\n",
            "export const unrelated = () => 4, load_rows = () => 3;\n",
            "moved",
        ),
        (
            "src/later_rec.ts",
            "export const unrelated = () => 3,\n  fetch_rows = (n: number): number => (n ? fetch_rows(n - 1) : 3);\n",
            "export const unrelated = () => 3,\n  load_rows = (n: number): number => (n ? load_rows(n - 1) : 3);\n",
            "moved",
        ),
        (
            "src/note_rename.ts",
            "export const fetch_rows = () => 3, /* unrelated: 3 */ unrelated = () => 3;\n",
            "export const load_rows = () => 3, /* unrelated: 4 */ unrelated = () => 4;\n",
            "moved",
        ),
        # a kind declared after another on its line
        (
            "src/alias.go",
            "package src\n\ntype ( Anchor = int; fetch_rows = int; Tail = int )\n",
            "package src\n\ntype ( Anchor = int; load_rows = int; Tail = int )\n",
            "moved",
        ),
        (
            "src/Anno.java",
            "@interface Anchor {} @interface fetch_rows { int value() default 3; }\n",
            "@interface Anchor {} @interface load_rows { int value() default 3; }\n",
            "moved",
        ),
        (
            "src/callable0.ts",
            "export const fetch_rows = (() => 3);\n",
            "export const load_rows = (() => 3);\n",
            "moved",
        ),
        (
            "src/callable1.ts",
            "export const fetch_rows = (() => 3) as () => number;\n",
            "export const load_rows = (() => 3) as () => number;\n",
            "moved",
        ),
        (
            "src/callable2.ts",
            "export const fetch_rows = function* () { yield 3; };\n",
            "export const load_rows = function* () { yield 3; };\n",
            "moved",
        ),
        (
            "src/callable3.ts",
            "export const fetch_rows = (function () { return 3; });\n",
            "export const load_rows = (function () { return 3; });\n",
            "moved",
        ),
        (
            "src/callable4.ts",
            "export const fetch_rows = (() => 3)!;\n",
            "export const load_rows = (() => 3)!;\n",
            "moved",
        ),
        (
            "src/callable5.ts",
            "export const fetch_rows = (() => 3) satisfies () => number;\n",
            "export const load_rows = (() => 3) satisfies () => number;\n",
            "moved",
        ),
        (
            "src/callable6.ts",
            "export const fetch_rows = async function* () { yield 3; };\n",
            "export const load_rows = async function* () { yield 3; };\n",
            "moved",
        ),
        (
            "src/callable7.ts",
            "export const fetch_rows = memo(() => 3);\n",
            "export const load_rows = memo(() => 3);\n",
            "moved",
        ),
        (
            "src/only.py",
            "class C:\n    def fetch_rows(self):\n        return 1\n",
            "class C:\n    def load_rows(self):\n        return 1\n",
            "moved",
        ),
        (
            "src/exp.ts",
            "export function fetch_rows(n: number): number {\n  return n ? fetch_rows(n - 1) : 1;\n}\n",
            "export function load_rows(n: number): number {\n  return n ? load_rows(n - 1) : 1;\n}\n",
            "moved",
        ),
    ],
)
async def test_imports_and_member_assignments_in_a_rename(env, path, before, after, state) -> None:
    repo = env
    c1 = repo.commit({path: before})
    await _index(TRUNK, repo, c1, [path])
    mid = await _remember(MEM, "rows", "`fetch_rows` returns one", files=[path])
    await _pass()
    await _relink_once(repo, path, after)
    assert [x["state"] for x in await _links(mid)] == [state]


async def test_a_delayed_cleanup_keeps_links_made_after_a_restore(env) -> None:
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemoryDeleteInput, MemoryRestoreInput
    from server.memory.app.handlers import handle_memory_delete
    from server.memory.app.restore import handle_memory_restore
    from server.memory.store import code_links as store

    await _drop_fetch_rows(env, TRUNK)
    ident = {"org": ORG, "namespace": MEM, "agent": "claude-code", "memory_key": "back"}
    mid = await _remember(MEM, "back", "`fetch_rows` returns n")
    await _pass()
    await handle_memory_delete(MemoryDeleteInput(**ident))

    class _RestoreBeforeDelete:
        def __init__(self, real: Any) -> None:
            self.real, self.done = real, False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.real, name)

        async def execute(self, sql: str, params: Any = None) -> Any:
            if not self.done and sql.startswith("DELETE FROM loom.memory_code_links"):
                self.done = True  # the cleanup decided; meanwhile the memory comes back
                await handle_memory_restore(MemoryRestoreInput(**ident))
                again = await _remember(MEM, "back", "`fetch_rows` returns n, always")
                assert again == mid
                await _pass()
            return await self.real.execute(sql, params=params)

    assert await store._drop_gone(_RestoreBeforeDelete(get_client()), {ORG: {mid}}) == 1
    assert [x["state"] for x in await _links(mid)] == ["fresh"]
    await _remove_fetch_rows(env, TRUNK)
    await _pass()
    assert "back" not in await _search(MEM, "fetch_rows returns n always")  # still linked


async def test_a_stale_rename_never_holds_the_current_links_back(env, monkeypatch) -> None:
    from server.code.domain.anchor import Link
    from server.memory.app import code_link_judge, code_link_pass, code_link_recheck
    from server.memory.store import code_links as store

    repo = env
    src = "def fetch_rows():\n    return 1\n\n\ndef store_rows():\n    return 2\n"
    c1 = repo.commit({"src/st.py": src})
    await _index(TRUNK, repo, c1, ["src/st.py"])
    await _set_policy(MEM, "revise")
    mid = await _remember(MEM, "st", "`fetch_rows` returns one")
    await _pass()
    (read,) = await store.for_memories(ORG, [mid])  # a recheck reads the old link
    c2 = repo.commit({"src/st.py": src.replace("fetch_rows", "load_rows")})
    await _index(TRUNK, repo, c2, ["src/st.py"])
    await _remember(MEM, "st", "`store_rows` returns two")  # rewritten and relinked
    await _pass()
    stale = code_link_recheck.Move(
        read, Link("src/st.py", "src.st::load_rows", read["body_hash"], "moved")
    )
    assert await code_link_recheck._current_moves(ORG, [stale]) == []  # the recheck drops it
    await code_link_recheck._write_moves([stale], c2)  # and if a race lets it in anyway
    calls: list[str] = []

    async def judge(claim: str, before: str, after: str) -> tuple[str, str]:
        calls.append(claim)
        return "contradicted", "`store_rows` returns three"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    c3 = repo.commit({"src/st.py": src.replace("fetch_rows", "load_rows").replace("2", "3")})
    await _index(TRUNK, repo, c3, ["src/st.py"])
    result = code_link_pass.PassResult()
    await code_link_recheck.recheck_all(result)  # the queue acknowledged the rewrite long ago
    assert not result.errors and calls == ["`store_rows` returns two"]
    links = await store.for_memories(ORG, [mid])
    assert [(x["qualified_name"].rsplit("::", 1)[-1], x["suggestion"]) for x in links] == [
        ("store_rows", "`store_rows` returns three")
    ]


@pytest.mark.parametrize(
    "imported", ["from fetch_rows import DEFAULT", "from backend import fetch_rows as handler"]
)
async def test_an_import_that_binds_another_name_keeps_a_pure_rename(env, imported) -> None:
    repo = env
    body = f"    {imported}\n    return fetch_rows(n - 1) if n else 0\n"
    c1 = repo.commit({"src/im.py": "def fetch_rows(n):\n" + body})
    await _index(TRUNK, repo, c1, ["src/im.py"])
    mid = await _remember(MEM, "im", "`fetch_rows` counts down to zero", files=["src/im.py"])
    await _pass()
    renamed = "def load_rows(n):\n" + body.replace("return fetch_rows(", "return load_rows(")
    await _relink_once(repo, "src/im.py", renamed)
    assert [x["state"] for x in await _links(mid)] == ["moved"]


async def _moved_to_another_repository(
    env: _Repo, tmp_path: Path, src: str, **delayed: Any
) -> tuple[str, _Repo, str]:
    """A memory linked in this repository, retagged to a second one defining the same
    path and name, and this repository's old link written late (with `delayed` over it):
    (memory id, the second repository, its namespace)."""
    import dataclasses

    from server.code.domain.index.remote import repo_tag
    from server.code.domain.index.repos import registry_repos, set_registry_repos
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set
    from server.memory.store import code_links as store

    other_remote, other_ns = "https://github.com/acme/gadgets.git", f"code-gadgets-{MEM[-6:]}"
    other = _Repo(tmp_path / "gadgets")
    gadgets = dataclasses.replace(
        registry_repos()[0],
        namespace=other_ns,
        repo="acme/gadgets",
        git_dir=str(other.root / ".git"),
        git_remote=other_remote,
        git_branch="main",
    )
    set_registry_repos([*registry_repos(), gadgets])
    await _index(TRUNK, env, env.commit({"src/io.py": src}), ["src/io.py"])
    await _index(other_ns, other, other.commit({"src/io.py": src}), ["src/io.py"])
    mid = await _remember(MEM, "io", "`load_rows` returns n")
    await _pass()
    (in_widgets,) = await store.for_memories(ORG, [mid])
    assert in_widgets["code_namespace"] == TRUNK
    await handle_memory_set(  # now about the other repository
        MemorySetInput(
            org=ORG,
            namespace=MEM,
            agent="claude-code",
            memory_key="io",
            value="`load_rows` returns n",
            tags=[repo_tag(other_remote)],
        )
    )
    await _pass()
    assert [x["code_namespace"] for x in await store.for_memories(ORG, [mid])] == [other_ns]
    await store.insert([store.link_row(in_widgets, **delayed)])  # the old link, late
    return mid, other, other_ns


async def test_a_stale_link_into_another_repository_never_reaches_its_policy(
    env, monkeypatch, tmp_path
) -> None:
    from server.memory.app import code_link_judge, code_link_pass, code_link_recheck
    from server.memory.store import code_links as store

    src = "def load_rows(n):\n    return n\n"  # the same path and name in both
    await _set_policy(MEM, "revise")
    mid, other, other_ns = await _moved_to_another_repository(env, tmp_path, src)
    calls: list[str] = []

    async def judge(claim: str, before: str, after: str) -> tuple[str, str]:
        calls.append(claim)
        return "contradicted", "`load_rows` returns n + 1"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    edited = src.replace("return n", "return n + 1")
    await _index(other_ns, other, other.commit({"src/io.py": edited}), ["src/io.py"])
    result = code_link_pass.PassResult()
    await code_link_recheck.recheck_all(result)  # the queue acknowledged the rewrite long ago
    assert not result.errors, result.errors
    links = await store.for_memories(ORG, [mid])
    assert calls == ["`load_rows` returns n"]
    assert [(x["code_namespace"], x["state"], x["suggestion"]) for x in links] == [
        (other_ns, "changed", "`load_rows` returns n + 1")
    ]


async def test_validate_counts_a_link_of_another_repository_only_when_current(
    env, tmp_path
) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models_namespace_catalog import NamespaceValidateInput
    from server.identity.app.namespace_validate import handle_namespace_validate
    from server.memory.store import code_links as store
    from server.operations.base import Role

    src = "def load_rows(n):\n    return n\n"
    mid, _, other_ns = await _moved_to_another_repository(env, tmp_path, src, state="changed")
    (current,) = [
        x for x in await store.for_memories(ORG, [mid]) if x["code_namespace"] == other_ns
    ]
    await store.insert([store.link_row(current, follows=int(current["version"]), state="changed")])
    set_current_auth(
        AuthInfo(
            subject="it@example.com",
            org=ORG,
            namespaces=(MEM, TRUNK, other_ns),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    try:
        report = await handle_namespace_validate(NamespaceValidateInput(org=ORG, namespace=MEM))
    finally:
        set_current_auth(None)
    assert next(f for f in report.findings if f.check == "code_links_stale").count == 1


@pytest.mark.parametrize(
    ("after", "state"),
    [
        # the decorator's argument is part of what the function is: not a rename alone
        ("@constant(load_rows)\ndef load_rows():\n    return 0\n", "changed"),
        ("@constant(fetch_rows)\ndef load_rows():\n    return 0\n", "moved"),
    ],
)
async def test_a_decorator_argument_is_not_a_reference_to_the_definition(env, after, state) -> None:
    c1 = env.commit({"src/dec.py": "@constant(fetch_rows)\ndef fetch_rows():\n    return 0\n"})
    await _index(TRUNK, env, c1, ["src/dec.py"])
    mid = await _remember(MEM, "dec", "`fetch_rows` returns zero", files=["src/dec.py"])
    await _pass()
    await _relink_once(env, "src/dec.py", after)
    assert [x["state"] for x in await _links(mid)] == [state]


@pytest.mark.parametrize(
    "helper",
    [
        "    def helper():\n        fetch_rows = 1\n        return fetch_rows\n",
        "    def helper(fetch_rows):\n        return fetch_rows\n",
    ],
)
async def test_a_name_a_nested_helper_binds_follows_a_rename_as_changed(env, helper) -> None:
    def src(name: str) -> str:
        return f"def {name}(n):\n{helper}    return {name}(n - 1) if n else helper(1)\n"

    c1 = env.commit({"src/nest.py": src("fetch_rows")})
    await _index(TRUNK, env, c1, ["src/nest.py"])
    mid = await _remember(MEM, "nest", "`fetch_rows` returns one", files=["src/nest.py"])
    await _pass()
    await _relink_once(env, "src/nest.py", src("load_rows"))
    # the name is bound inside too: the calls may mean something else, so it is not a
    # rename alone; the memory stays served, flagged
    assert [x["state"] for x in await _links(mid)] == ["changed"]
    assert await _search(MEM, "fetch_rows returns one") == {"nest": "changed"}


@pytest.mark.parametrize(
    ("before", "after", "state"),
    [
        # the walrus target is the function's local: `load_rows` returns itself now, so
        # the link follows the rename only as changed
        (
            "def fetch_rows():\n    [(fetch_rows := 3) for _ in [0]]\n    return fetch_rows\n",
            "def load_rows():\n    [(fetch_rows := 3) for _ in [0]]\n    return load_rows\n",
            "changed",
        ),
        # a nested helper's default names the function beside a local of that name
        (
            "def fetch_rows(n):\n    def helper(cb=fetch_rows):\n        fetch_rows = lambda: 9\n"
            "        return cb(0)\n    return helper() if n else 3\n",
            "def load_rows(n):\n    def helper(cb=load_rows):\n        fetch_rows = lambda: 9\n"
            "        return cb(0)\n    return helper() if n else 3\n",
            "changed",
        ),
        # the name is also a default: followed, but as changed
        (
            "def fetch_rows(n, callback=fetch_rows):\n    return fetch_rows(n - 1) if n else callback\n",
            "def load_rows(n, callback=fetch_rows):\n    return load_rows(n - 1) if n else callback\n",
            "changed",
        ),
        # a rename alone
        (
            "def fetch_rows(n):\n    return fetch_rows(n - 1) if n else 3\n",
            "def load_rows(n):\n    return load_rows(n - 1) if n else 3\n",
            "moved",
        ),
    ],
)
async def test_a_rename_is_matched_only_when_the_names_mean_the_same(
    env, before, after, state
) -> None:
    c1 = env.commit({"src/wal.py": before})
    await _index(TRUNK, env, c1, ["src/wal.py"])
    mid = await _remember(MEM, "wal", "`fetch_rows` returns three", files=["src/wal.py"])
    await _pass()
    await _relink_once(env, "src/wal.py", after)
    assert [x["state"] for x in await _links(mid)] == [state]


@pytest.mark.parametrize(
    ("path", "src"),
    [
        (
            "src/alias.cpp",
            "struct Box { operator int() { return 3; } };\n\nint NAME(int n = 0) {\n"
            "    if (!n) return 4;\n    using fetch_rows = Box;\n    return NAME();\n}\n",
        ),
        (
            "src/local.cpp",
            "int NAME(int n = 0) {\n    if (!n) return 4;\n"
            "    struct fetch_rows { operator int() { return 3; } };\n    return NAME();\n}\n",
        ),
        (
            "src/local.ts",
            "export function NAME(n: number = 0): number {\n  if (!n) return 4;\n"
            "  class fetch_rows { get v(): number { return 3; } }\n"
            "  return (NAME.prototype as any).v;\n}\n",
        ),
    ],
)
async def test_a_type_declared_with_the_name_may_take_it(env, path, src) -> None:
    from server.memory.store import code_links as store

    c1 = env.commit({path: src.replace("NAME", "fetch_rows")})
    await _index(TRUNK, env, c1, [path])
    mid = await _remember(MEM, "type", "`fetch_rows` returns three for n=1", files=[path])
    await _pass()
    baseline = [x["body_hash"] for x in await store.for_memories(ORG, [mid])]
    assert len(baseline) == 1
    await _relink_once(env, path, src.replace("NAME", "load_rows"))
    links = await store.for_memories(ORG, [mid])
    assert [x["state"] for x in links] == ["changed"]
    assert [x["body_hash"] for x in links] == baseline


async def test_a_namesake_on_the_same_line_never_lends_its_shape(env) -> None:
    before = "namespace A { int fetch_rows() { return 1; } } namespace B { int fetch_rows() { return 3; } }\n"
    after = "namespace A { int fetch_rows() { return 1; } } namespace B { int load_rows() { return 4; } }\n"
    c1 = env.commit({"src/ab.cpp": before})
    await _index(TRUNK, env, c1, ["src/ab.cpp"])
    mid = await _remember(MEM, "ab", "`B::fetch_rows` returns three", files=["src/ab.cpp"])
    await _pass()
    await _relink_once(env, "src/ab.cpp", after)
    (link,) = await _links(mid)
    assert (link["symbol"], link["state"]) == ("B::load_rows", "changed")


async def test_a_qualified_call_on_the_line_does_not_hide_a_rename(env) -> None:
    def src(name: str) -> str:
        return (
            "namespace backend { int fetch_rows() { return 7; } }\nnamespace DB {\n"
            f"int unrelated() {{ return backend::fetch_rows(); }} int {name}() {{ return 3; }}\n}}\n"
        )

    c1 = env.commit({"src/q.cpp": src("fetch_rows")})
    await _index(TRUNK, env, c1, ["src/q.cpp"])
    mid = await _remember(MEM, "q", "`DB::fetch_rows` returns three", files=["src/q.cpp"])
    await _pass()
    await _relink_once(env, "src/q.cpp", src("load_rows"))
    assert [x["state"] for x in await _links(mid)] == ["moved"]


async def test_a_removed_method_never_moves_to_a_namesake_in_another_class(env) -> None:
    from server.memory.store import code_links as store

    reader = "class Reader:\n    limit = 3\n\n    def read_rows(self):\n        return self.limit\n\n    def other(self):\n        return 1\n"
    c1 = env.commit({"src/rw.py": reader})
    await _index(TRUNK, env, c1, ["src/rw.py"])
    mid = await _remember(MEM, "rw", "`Reader.read_rows` returns three", files=["src/rw.py"])
    await _pass()
    assert [x["qualified_name"] for x in await store.for_memories(ORG, [mid])] == [
        "src.rw::Reader::read_rows"
    ]
    moved = (
        "class Reader:\n    limit = 3\n\n    def other(self):\n        return 1\n\n\n"
        "class Writer:\n    limit = 4\n\n    def read_rows(self):\n        return self.limit\n"
    )
    await _relink_once(env, "src/rw.py", moved)
    links = await store.for_memories(ORG, [mid])
    assert [(x["qualified_name"], x["state"]) for x in links] == [
        ("src.rw::Reader::read_rows", "missing")
    ]
    assert await _lessons(TRUNK, "Writer.read_rows") == []


@pytest.mark.parametrize("path", ["io.cpp", "io/reader.cpp"])
async def test_a_namespace_named_like_its_file_is_part_of_the_name(env, path) -> None:
    before = (
        "namespace io {\nconst int limit = 3;\nint fetch_rows() { return limit; }\nint ping() { return 1; }\n}\n"
        "const int limit = 4;\n"
    )
    after = (
        "namespace io {\nconst int limit = 3;\nint ping() { return 1; }\n}\n"
        "const int limit = 4;\nint fetch_rows() { return limit; }\n"
    )
    c1 = env.commit({path: before})
    await _index(TRUNK, env, c1, [path])
    mid = await _remember(MEM, "ns", "`io::fetch_rows` returns three", files=[path])
    await _pass()
    await _relink_once(env, path, after)
    assert [x["state"] for x in await _links(mid)] == ["missing"]


async def test_a_file_move_keeps_the_namespace_in_the_name(env) -> None:
    c1 = env.commit(
        {"io.cpp": "namespace io {\nconst int limit = 3;\nint fetch_rows() { return limit; }\n}\n"}
    )
    await _index(TRUNK, env, c1, ["io.cpp"])
    mid = await _remember(MEM, "mv", "`io::fetch_rows` returns three", files=["io.cpp"])
    await _pass()
    c2 = env.commit(
        {"db.cpp": "namespace db {\nconst int limit = 4;\nint fetch_rows() { return limit; }\n}\n"},
        mv=("io.cpp", "db.cpp"),
    )
    await _index(TRUNK, env, c2, ["io.cpp", "db.cpp"])
    await _pass()
    assert [x["state"] for x in await _links(mid)] == ["missing"]


@pytest.mark.parametrize(
    "box",
    [
        "        enum Box {\n            INSTANCE;\n            int fetch_rows(int m) { return 3; }\n"
        "            int apply() { return NAME(0); }\n        }\n        return Box.INSTANCE.apply();\n",
        "        interface Box {\n            default int fetch_rows(int m) { return 3; }\n"
        "            default int apply() { return NAME(0); }\n        }\n"
        "        return new Box() {}.apply();\n",
    ],
)
async def test_a_local_enum_or_interface_may_take_the_name(env, box) -> None:
    from server.memory.store import code_links as store

    def src(name: str) -> str:
        body = box.replace("NAME", name)
        return f"class Sample {{\n    static int {name}(int n) {{\n        if (n == 0) return 4;\n{body}    }}\n}}\n"

    c1 = env.commit({"src/Sample.java": src("fetch_rows")})
    await _index(TRUNK, env, c1, ["src/Sample.java"])
    mid = await _remember(
        MEM, "box", "`Sample.fetch_rows` returns three", files=["src/Sample.java"]
    )
    await _pass()
    baseline = [x["body_hash"] for x in await store.for_memories(ORG, [mid])]
    await _relink_once(env, "src/Sample.java", src("load_rows"))
    links = await store.for_memories(ORG, [mid])
    assert [x["state"] for x in links] == ["changed"]
    assert [x["body_hash"] for x in links] == baseline


@pytest.mark.parametrize(
    ("path", "src"),
    [
        ("reader.py", "def other():\n    return 0\n\n\ndef NAME():\n    return RV\n"),
        ("reader.c", "int other(void) { return 0; }\n\nint NAME(void) { return RV; }\n"),
        ("reader.cpp", "int other() { return 0; }\n\nint NAME() { return RV; }\n"),
        (
            "reader.ts",
            "export function other(): number { return 0; }\n\nexport function NAME(): number { return RV; }\n",
        ),
        (
            "reader.js",
            "export function other() { return 0; }\n\nexport function NAME() { return RV; }\n",
        ),
        (
            "reader.go",
            "package reader\n\nfunc other() int { return 0 }\n\nfunc NAME() int { return RV }\n",
        ),
        ("reader.rs", "pub fn other() -> u32 { 0 }\n\npub fn NAME() -> u32 { RV }\n"),
        (
            "Reader.java",
            "class Reader {\n  int other() { return 0; }\n  int NAME() { return RV; }\n}\n",
        ),
    ],
)
async def test_a_rename_with_an_edit_follows_the_new_name_as_changed(env, path, src) -> None:
    repo = env
    c1 = repo.commit({path: src.replace("NAME", "fetch_rows").replace("RV", "1")})
    await _index(TRUNK, repo, c1, [path])
    mid = await _remember(MEM, "review", "`fetch_rows` requires review.", files=[path])
    await _pass()
    await _relink_once(repo, path, src.replace("NAME", "load_rows").replace("RV", "2"))
    [link] = await _links(mid)
    assert link["state"] == "changed" and link["symbol"].replace("::", ".").endswith("load_rows")
    assert (await _search(MEM, "fetch_rows requires review")).get("review") == "changed"


@pytest.mark.parametrize(
    ("src", "a", "b"),
    [
        ("package reader\n\nfunc fetch_rows() int { return 1 }\n", "reader.go", "sub/reader.go"),
        ("package reader\n\nfunc fetch_rows() int { return 1 }\n", "sub/reader.go", "reader.go"),
        ("pub fn fetch_rows() -> u32 { 1 }\n", "reader.rs", "sub/reader.rs"),
        ("pub fn fetch_rows() -> u32 { 1 }\n", "sub/reader.rs", "reader.rs"),
    ],
)
async def test_a_go_or_rust_file_moved_across_directories_is_moved(env, src, a, b) -> None:
    c1 = env.commit({a: src})
    await _index(TRUNK, env, c1, [a])
    mid = await _remember(MEM, "mv", "`fetch_rows` requires review.", files=[a])
    await _pass()
    c2 = env.commit({}, mv=(a, b))
    await _index(TRUNK, env, c2, [a, b])
    await _pass()
    assert [(x["state"], x["path"]) for x in await _links(mid)] == [("moved", b)]


async def test_a_go_package_renamed_with_its_directory_is_moved(env) -> None:
    src = "package reader\n\nfunc fetch_rows() int { return 1 }\n"
    c1 = env.commit({"reader/r.go": src})
    await _index(TRUNK, env, c1, ["reader/r.go"])
    mid = await _remember(MEM, "pkg", "`fetch_rows` requires review.", files=["reader/r.go"])
    await _pass()
    renamed = src.replace("package reader", "package rows")
    c2 = env.commit({"rows/r.go": renamed}, mv=("reader/r.go", "rows/r.go"))
    await _index(TRUNK, env, c2, ["reader/r.go", "rows/r.go"])
    await _pass()
    assert [(x["state"], x["path"]) for x in await _links(mid)] == [("moved", "rows/r.go")]


async def test_code_that_may_have_been_renamed_is_unresolved_and_still_served(env) -> None:
    kept = "def other():\n    return 0\n"
    c1 = env.commit(
        {"src/u.py": kept + "\n\ndef fetch_rows(data):\n    return sorted(data)[:10]\n"}
    )
    await _index(TRUNK, env, c1, ["src/u.py"])
    mid = await _remember(MEM, "u", "`fetch_rows` keeps the first ten", files=["src/u.py"])
    await _pass()
    await _relink_once(env, "src/u.py", kept + "\n\ndef warm_up(host):\n    connect(host).ping()\n")
    assert [x["state"] for x in await _links(mid)] == ["unresolved"]
    assert (await _search(MEM, "fetch_rows keeps the first ten")).get("u") == "unresolved"


async def test_an_unresolved_link_stays_unresolved_across_an_unrelated_commit(env) -> None:
    kept = "def other():\n    return 0\n"
    gone = kept + "\n\ndef fetch_rows(data):\n    return sorted(data)[:10]\n"
    c1 = env.commit({"src/u.py": gone, "src/z.py": "Z = 1\n"})
    await _index(TRUNK, env, c1, ["src/u.py", "src/z.py"])
    mid = await _remember(MEM, "u", "`fetch_rows` keeps the first ten", files=["src/u.py"])
    await _pass()
    await _relink_once(env, "src/u.py", kept + "\n\ndef warm_up(host):\n    connect(host).ping()\n")
    assert [x["state"] for x in await _links(mid)] == ["unresolved"]
    # the evidence is weighed against the commit that still had the code, not the last one
    await _relink_once(env, "src/z.py", "Z = 2\n")
    assert [x["state"] for x in await _links(mid)] == ["unresolved"]
    assert (await _search(MEM, "fetch_rows keeps the first ten")).get("u") == "unresolved"


async def test_an_unresolved_link_follows_its_file_through_a_rename(env) -> None:
    kept = "def other():\n    return 0\n"
    gone = kept + "\n\ndef fetch_rows(data):\n    return sorted(data)[:10]\n"
    c1 = env.commit({"src/u.py": gone})
    await _index(TRUNK, env, c1, ["src/u.py"])
    mid = await _remember(MEM, "u", "`fetch_rows` keeps the first ten", files=["src/u.py"])
    await _pass()
    await _relink_once(env, "src/u.py", kept + "\n\ndef warm_up(host):\n    connect(host).ping()\n")
    assert [x["state"] for x in await _links(mid)] == ["unresolved"]
    # over the whole span git no longer pairs the file; commit by commit it does
    c3 = env.commit({}, mv=("src/u.py", "src/v.py"))
    await _index(TRUNK, env, c3, ["src/u.py", "src/v.py"])
    await _pass()
    assert [(x["state"], x["path"]) for x in await _links(mid)] == [("unresolved", "src/v.py")]
    await _relink_once(env, "src/z.py", "Z = 2\n")
    assert [x["state"] for x in await _links(mid)] == ["unresolved"]


async def test_an_identical_copy_there_all_along_never_takes_a_removed_function(env) -> None:
    helper = "def fetch_rows():\n    return 1\n"
    keep = "def keep():\n    return 0\n"
    c1 = env.commit({"src/a.py": helper + "\n\n" + keep, "src/b.py": helper})
    await _index(TRUNK, env, c1, ["src/a.py", "src/b.py"])
    mid = await _remember(MEM, "copy", "`fetch_rows` returns one", files=["src/a.py"])
    await _pass()
    assert [(x["state"], x["path"]) for x in await _links(mid)] == [("fresh", "src/a.py")]
    await _relink_once(env, "src/a.py", keep)
    assert [(x["state"], x["path"]) for x in await _links(mid)] == [("missing", "src/a.py")]


async def test_a_namespace_renamed_with_its_file_never_moves_a_link_unchanged(env) -> None:
    def program(ns: str, limit: int) -> str:
        return f"namespace {ns} {{\nconst int limit = {limit};\nint fetch_rows() {{ return limit; }}\n}}\n"

    c1 = env.commit({"io.cpp": program("io", 3)})
    await _index(TRUNK, env, c1, ["io.cpp"])
    mid = await _remember(MEM, "ns", "`io::fetch_rows` returns three", files=["io.cpp"])
    await _pass()
    c2 = env.commit({"db.cpp": program("db", 4)}, mv=("io.cpp", "db.cpp"))
    await _index(TRUNK, env, c2, ["io.cpp", "db.cpp"])
    await _pass()
    assert [x["state"] for x in await _links(mid)] != ["moved"]


_CONTRACT = (
    "`fetch_rows` is part of a compatibility contract. Maintainers require a deprecated "
    "wrapper under its old public name for two releases after a rename."
)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "retained = []\ndef fetch_rows(payload):\n    buffer = bytearray(payload)\n"
            "    retained.append(buffer)\n    return len(buffer)\n",
            "retained = []\ndef load_rows(payload):\n    audit_count = 0\n"
            "    buffer = bytearray(payload)\n    retained.append(buffer)\n    return len(buffer)\n",
        ),
        (
            "def fetch_rows(lock, operation):\n    lock.acquire()\n    result = operation()\n"
            "    lock.release()\n    return result\n",
            "def load_rows(lock, operation):\n    audit_count = 0\n    lock.acquire()\n"
            "    result = operation()\n    lock.release()\n    return result\n",
        ),
    ],
)
async def test_a_contract_lesson_follows_a_rename_with_a_small_edit(env, before, after) -> None:
    c1 = env.commit({"reader.py": before})
    await _index(TRUNK, env, c1, ["reader.py"])
    mid = await _remember(MEM, "contract", _CONTRACT, files=["reader.py"])
    await _pass()
    await _relink_once(env, "reader.py", after)
    (link,) = await _links(mid)
    assert (link["symbol"], link["state"]) == ("reader::load_rows", "changed")
    # still found, marked, on the current name, and its text as written
    hits = await _search(MEM, "compatibility contract deprecated wrapper old public name")
    assert hits.get("contract") == "changed"
    lessons = await _lessons(TRUNK, "load_rows")
    assert [(x["memory_key"], x["code_state"]) for x in lessons] == [("contract", "changed")]
    assert lessons[0]["excerpt"] == _CONTRACT  # the text, not its stored JSON
    assert (await _row(mid)).value == _CONTRACT


async def test_a_class_renamed_around_a_method_keeps_it_changed_after_later_commits(env) -> None:
    def program(cls: str, limit: int, ping: int) -> str:
        return (
            f"export class {cls} {{\n  limit = {limit};\n"
            "  fetch_rows(): number { return this.limit; }\n"
            f"  ping(): number {{ return {ping}; }}\n}}\n"
        )

    c1 = env.commit({"src/r.ts": program("Reader", 3, 1)})
    await _index(TRUNK, env, c1, ["src/r.ts"])
    mid = await _remember(MEM, "limit", "`Reader.fetch_rows` returns three", files=["src/r.ts"])
    await _pass()
    await _relink_once(env, "src/r.ts", program("Writer", 4, 1))
    await _relink_once(env, "src/r.ts", program("Writer", 4, 2))
    (link,) = await _links(mid)
    assert (link["symbol"], link["state"]) == ("src.r::Writer::fetch_rows", "changed")
    assert (await _search(MEM, "fetch_rows returns three")).get("limit") == "changed"
    lessons = await _lessons(TRUNK, "Writer.fetch_rows")
    assert [(x["memory_key"], x["code_state"]) for x in lessons] == [("limit", "changed")]


async def test_a_method_of_a_class_renamed_alone_is_moved(env) -> None:
    def program(cls: str) -> str:
        return (
            f"class {cls}:\n    def size(self):\n        return 3\n\n"
            "    def close(self):\n        return None\n"
        )

    c1 = env.commit({"src/r.py": program("Reader")})
    await _index(TRUNK, env, c1, ["src/r.py"])
    mid = await _remember(MEM, "size", "`Reader.size` returns three")
    await _pass()
    await _relink_once(env, "src/r.py", program("RowReader"))
    (link,) = await _links(mid)
    assert (link["symbol"].rsplit("::", 2)[-2:], link["state"]) == (["RowReader", "size"], "moved")


async def test_a_host_without_history_serves_lessons_as_trunk_last_checked_them(
    env, monkeypatch
) -> None:
    from server.code.app import memory_links as code
    from server.settings import settings

    c1 = env.commit({"src/r.py": "def fetch_rows():\n    return 1\n"})
    await _index(TRUNK, env, c1, ["src/r.py"])
    await _remember(MEM, "rows", "`fetch_rows` returns one")
    await _pass()
    await _relink_once(env, "src/r.py", "def fetch_rows():\n    return 2\n")

    async def no_git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LOOM_GIT_HISTORY=none reads no git")

    monkeypatch.setattr(settings, "git_history", "none")
    monkeypatch.setattr(code, "parsed", no_git)
    lessons = await _lessons(TRUNK, "fetch_rows")
    assert [(x["memory_key"], x["code_state"]) for x in lessons] == [("rows", "changed")]


async def test_a_method_its_renamed_class_carried_is_never_judged(env, monkeypatch) -> None:
    from server.memory.app import code_link_judge

    def program(cls: str, limit: int) -> str:
        return (
            f"export class {cls} {{\n  limit = {limit};\n"
            "  fetch_rows(): number { return this.limit; }\n"
            "  ping(): number { return 1; }\n}\n"
        )

    c1 = env.commit({"src/w.ts": program("Reader", 3)})
    await _index(TRUNK, env, c1, ["src/w.ts"])
    await _set_policy(MEM, "revise")
    claim = f"`Reader.fetch_rows` returns three ({MEM})"
    mid = await _remember(MEM, "carried", claim, files=["src/w.ts"])
    await _pass()
    calls: list[str] = []

    async def judge(judged: str, before: str, after: str) -> tuple[str, str]:
        calls.append(judged)
        return "holds", ""  # the method's text alone reads the same

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _relink_once(env, "src/w.ts", program("Writer", 4))
    await _pass()
    (link,) = await _links(mid)
    assert (link["state"], link["suggestion"]) == ("changed", "")
    assert claim not in calls
    lessons = await _lessons(TRUNK, "Writer.fetch_rows")
    assert [(x["memory_key"], x["code_state"]) for x in lessons] == [("carried", "changed")]


@pytest.mark.parametrize("labelled", ["at_write", "after_linking"])
async def test_a_high_sensitivity_memory_is_never_judged(env, monkeypatch, labelled) -> None:
    from server.memory.app import code_link_judge

    c1 = env.commit({"src/s.py": "def secret_rows():\n    return 1\n"})
    await _index(TRUNK, env, c1, ["src/s.py"])
    await _set_policy(MEM, "revise")
    claim = f"`secret_rows` returns one ({MEM})"
    first = {"sensitivity": "high"} if labelled == "at_write" else {}
    mid = await _remember(MEM, "secret", claim, **first)
    await _pass()
    if labelled == "after_linking":
        assert await _remember(MEM, "secret", claim, sensitivity="high") == mid
        await _pass()
    calls: list[str] = []

    async def judge(judged: str, before: str, after: str) -> tuple[str, str]:
        calls.append(judged)
        return "contradicted", "`secret_rows` returns two"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _relink_once(env, "src/s.py", "def secret_rows():\n    return 2\n")
    await _pass()
    assert calls == []  # its value never reaches the judge's model
    (link,) = await _links(mid)
    assert (link["state"], link["suggestion"]) == ("changed", "")


@pytest.mark.parametrize("names", [["secret_rows"], ["secret_rows", "secret_keys"]])
async def test_a_memory_labelled_high_while_it_is_judged_is_judged_no_further(
    env, monkeypatch, names
) -> None:
    from server.memory.app import code_link_judge

    def program(value: int) -> str:
        return "".join(f"def {n}():\n    return {value}\n\n\n" for n in names)

    c1 = env.commit({"src/s.py": program(1)})
    await _index(TRUNK, env, c1, ["src/s.py"])
    await _set_policy(MEM, "revise")
    claim = " and ".join(f"`{n}` returns one" for n in names) + f" ({MEM})"
    mid = await _remember(MEM, "secret", claim)
    await _pass()
    calls: list[str] = []

    async def judge(judged: str, before: str, after: str) -> tuple[str, str]:
        calls.append(judged)
        await _remember(MEM, "secret", claim, sensitivity="high")  # labelled meanwhile
        return "contradicted", "they return two"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _relink_once(env, "src/s.py", program(2))
    await _pass()
    assert len(calls) == 1  # no judgement starts once it is high
    assert [(x["state"], x["suggestion"]) for x in await _links(mid)] == [("changed", "")] * len(
        names
    )


async def test_a_class_rename_drops_the_suggestion_its_method_carried(env, monkeypatch) -> None:
    from server.memory.api.models_crud import MemoryGetInput
    from server.memory.app import code_link_judge
    from server.memory.app.handlers import handle_memory_get

    def program(cls: str, limit: int, body: str) -> str:
        return (
            f"export class {cls} {{\n  limit = {limit};\n"
            f"  fetch_rows(): number {{ return {body}; }}\n"
            "  ping(): number { return 1; }\n}\n"
        )

    c1 = env.commit({"src/k.ts": program("Source", 3, "0")})
    await _index(TRUNK, env, c1, ["src/k.ts"])
    await _set_policy(MEM, "revise")
    claim = f"`Source.fetch_rows` returns zero ({MEM})"
    mid = await _remember(MEM, "suggested", claim, files=["src/k.ts"])
    await _pass()

    async def judge(judged: str, before: str, after: str) -> tuple[str, str]:
        return "contradicted", "`fetch_rows` returns three"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _relink_once(env, "src/k.ts", program("Source", 3, "this.limit"))
    assert [x["suggestion"] for x in await _links(mid)] == ["`fetch_rows` returns three"]
    # the class renamed and its field edited: the method's own text is the same
    await _relink_once(env, "src/k.ts", program("Sink", 4, "this.limit"))
    (link,) = await _links(mid)
    assert (link["state"], link["suggestion"]) == ("changed", "")
    got = await handle_memory_get(
        MemoryGetInput(org=ORG, namespace=MEM, agent="claude-code", memory_key="suggested")
    )
    assert [c.suggestion for c in got.code] == [""]
    (lesson,) = await _lessons(TRUNK, "Sink.fetch_rows")
    assert (lesson["code_state"], lesson["suggestion"]) == ("changed", "")


async def test_the_judge_reads_the_overload_signature_that_was_removed(env, monkeypatch) -> None:
    from server.memory.app import code_link_judge

    signatures = (
        "export function load(a: number): number;\nexport function load(a: string): string;\n"
    )
    body = "export function load(a: any): any {\n  return a;\n}\n"
    c1 = env.commit({"src/o.ts": signatures + body})
    await _index(TRUNK, env, c1, ["src/o.ts"])
    await _set_policy(MEM, "revise")
    claim = f"`load` accepts a string ({MEM})"
    mid = await _remember(MEM, "overload", claim, files=["src/o.ts"])
    await _pass()
    seen: list[tuple[str, str]] = []

    async def judge(judged: str, before: str, after: str) -> tuple[str, str]:
        if judged == claim:
            seen.append((before, after))
        return "contradicted", "`load` accepts only a number"

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    await _relink_once(env, "src/o.ts", signatures.split("\n")[0] + "\n" + body)
    ((before, after),) = seen
    assert "load(a: string)" in before and "load(a: string)" not in after
    (link,) = await _links(mid)
    assert (link["state"], link["suggestion"]) == ("changed", "`load` accepts only a number")


async def test_a_branch_with_the_renamed_class_keeps_its_lesson_changed(env) -> None:
    def program(cls: str, limit: int) -> str:
        return (
            f"export class {cls} {{\n  limit = {limit};\n"
            "  fetch_rows(): number { return this.limit; }\n"
            "  ping(): number { return 1; }\n}\n"
        )

    c1 = env.commit({"src/r.ts": program("Reader", 3)})
    for ns in (TRUNK, BRANCH):
        await _index(ns, env, c1, ["src/r.ts"])
    await _remember(MEM, "limit", "`Reader.fetch_rows` returns three", files=["src/r.ts"])
    await _pass()
    c2 = await _relink_once(env, "src/r.ts", program("Writer", 4))
    # a branch that kept `Reader` still has the code the memory describes
    assert [
        (x["memory_key"], x["code_state"]) for x in await _lessons(BRANCH, "Reader.fetch_rows")
    ] == [("limit", "fresh")]
    # a branch with trunk's `Writer` has the same method text in a changed class
    await _index(BRANCH, env, c2, ["src/r.ts"])
    for ns in (TRUNK, BRANCH):
        assert [
            (x["memory_key"], x["code_state"]) for x in await _lessons(ns, "Writer.fetch_rows")
        ] == [("limit", "changed")]


async def test_for_file_reads_links_only_of_the_repository_the_call_names(env) -> None:
    from server.code.domain.index.remote import repo_tag
    from server.memory.api.models_for_file import MemoryForFileInput
    from server.memory.app.for_file import handle_memory_for_file

    c1 = env.commit({"src/k.py": "def kept_rows():\n    return 1\n"})
    await _index(TRUNK, env, c1, ["src/k.py"])
    await _remember(MEM, "kept", "`kept_rows` returns one")
    await _pass()

    async def code_state(**kw: Any) -> str | None:
        out = await handle_memory_for_file(
            MemoryForFileInput(org=ORG, namespace=MEM, path="src/k.py", **kw)
        )
        return next((h.code_state for h in out.hits if h.memory_key == "kept"), None)

    assert await code_state(tags=[repo_tag(REMOTE)]) == "fresh"
    # a path alone names no repository: another repository's `src/k.py` lends nothing
    assert await code_state() is None


async def test_a_function_moved_unchanged_into_another_module_is_moved(env) -> None:
    body = "def fetch_rows(n):\n    return n\n"
    c1 = env.commit({"pkg/a.py": body + "\n\ndef other():\n    return 0\n", "pkg/b.py": "X = 1\n"})
    await _index(TRUNK, env, c1, ["pkg/a.py", "pkg/b.py"])
    mid = await _remember(MEM, "mv", "`fetch_rows` returns n", files=["pkg/a.py"])
    await _pass()
    c2 = env.commit({"pkg/a.py": "def other():\n    return 0\n", "pkg/b.py": "X = 1\n\n\n" + body})
    await _index(TRUNK, env, c2, ["pkg/a.py", "pkg/b.py"])
    await _pass()
    assert [(x["state"], x["path"]) for x in await _links(mid)] == [("moved", "pkg/b.py")]


async def test_for_file_serves_a_memory_while_any_of_its_links_there_is_alive(env) -> None:
    from server.code.domain.index.remote import repo_tag
    from server.memory.api.models_for_file import MemoryForFileInput
    from server.memory.app.for_file import handle_memory_for_file

    both = "def fetch_rows():\n    return 1\n\n\ndef keep_rows():\n    return 2\n"
    c1 = env.commit({"src/a.py": both})
    await _index(TRUNK, env, c1, ["src/a.py"])
    await _remember(MEM, "pair", "`fetch_rows` and `keep_rows` agree")
    await _pass()
    await _relink_once(env, "src/a.py", "def keep_rows():\n    return 2\n")
    out = await handle_memory_for_file(
        MemoryForFileInput(org=ORG, namespace=MEM, path="src/a.py", tags=[repo_tag(REMOTE)])
    )
    # served for the code still there, shown with the worst of its links
    assert [(h.memory_key, h.code_state) for h in out.hits] == [("pair", "missing")]


async def test_a_change_survives_a_stop_in_unresolved(env) -> None:
    keep = "def keep():\n    return 0\n"
    rows = "\n\ndef NAME(data):\n    return sorted(data, key=len)[:LIMIT]\n"
    c0 = env.commit({"src/m.py": keep + rows.replace("NAME", "fetch_rows").replace("LIMIT", "10")})
    await _index(TRUNK, env, c0, ["src/m.py"])
    mid = await _remember(MEM, "ten", "`fetch_rows` keeps the first ten", files=["src/m.py"])
    await _pass()
    edited = keep + rows.replace("NAME", "fetch_rows").replace("LIMIT", "20")
    await _relink_once(env, "src/m.py", edited)
    assert [x["state"] for x in await _links(mid)] == ["changed"]
    # renamed beside a near-twin: not sure which is it
    renamed = rows.replace("NAME", "load_rows").replace("LIMIT", "20")
    twin = rows.replace("NAME", "load_rows_fast").replace("LIMIT", "20")
    await _relink_once(env, "src/m.py", keep + renamed + twin)
    assert [x["state"] for x in await _links(mid)] == ["unresolved"]
    # the twin goes: the claim still describes code that changed
    await _relink_once(env, "src/m.py", keep + renamed)
    (link,) = await _links(mid)
    assert (link["symbol"], link["state"]) == ("src.m::load_rows", "changed")


async def test_the_sweep_reads_page_after_page(env, monkeypatch) -> None:
    from server.memory.store import code_links as store

    monkeypatch.setattr(store, "SWEEP_ROWS_PER_WRITE", 1)
    repo = env
    c1 = repo.commit({"src/widgets/reader.py": _READER})
    await _index(TRUNK, repo, c1, ["src/widgets/reader.py"])
    mids = [await _remember(MEM, f"swept-{i}", f"`Reader.read_rows` note {i}") for i in range(3)]
    await _pass()
    # one row a page through this repository's rows, each page after the last (earlier
    # tests' memories share the remote, so some of them are linked here too)
    after: tuple[str, ...] | None = (ORG, TRUNK, "", "", "", "00000000-0000-0000-0000-000000000000")
    pages = []
    while after is not None and after[:2] == (ORG, TRUNK):
        pages.append(after)
        after, dropped = await store.sweep_page(after, 1)
        assert dropped == 0
    # no page twice (ClickHouse orders a UUID by its value, not its text)
    assert len(pages) == len(set(pages)) and set(mids) <= {page[-1] for page in pages}


async def test_a_memory_stored_without_a_user_or_session_keeps_its_links_through_backfill(
    env, ch_client, monkeypatch
) -> None:
    from server.code.app import memory_links
    from server.memory.workers.code_links import run_pass
    from server.settings import settings

    repo = env
    c1 = repo.commit({"src/old.py": "def legacy_value():\n    return 8\n"})
    await _index(TRUNK, repo, c1, ["src/old.py"])
    mid = await _remember(MEM, "anon", "`legacy_value` is eight", user_id="", session_id="")
    await _pass()
    assert await _links(mid)
    monkeypatch.setattr(settings, "memory_code_links_batch", 1)  # one identity a page
    await memory_links.set_link_cursor("backfill", "")
    for _ in range(50):
        await run_pass()
        if await memory_links.link_cursor("backfill") == _BACKFILL_DONE:
            break
    assert [link["symbol"].rsplit("::", 1)[-1] for link in await _links(mid)] == ["legacy_value"]


async def test_redaction_blanks_suggestions_not_made_from_the_live_value(env, ch_client) -> None:
    from server.core.ch import get_client
    from server.memory.store import code_links as store
    from server.memory.store.redaction import key_derivative_scrubs, run_scrubs

    ident = {"org": ORG, "namespace": MEM, "agent": "a", "user_id": "u", "session_id": "s"}
    old = store.link_row(
        **ident,
        memory_id="00000000-0000-0000-0000-0000000000a1",
        memory_key="k",
        path="a.py",
        inputs_hash="older-value",
        suggestion="REWRITE OF THE SECRET TEXT",
        version=100,
    )
    live = store.link_row(
        **ident,
        memory_id="00000000-0000-0000-0000-0000000000a1",
        memory_key="k",
        path="b.py",
        inputs_hash="live-value",
        suggestion="about the live text",
        version=100,
    )
    # written as they are, without the insert's check for a memory behind them
    await get_client().insert(store.TABLE, [old, live])
    scrubs = await key_derivative_scrubs(
        ORG, MEM, "a", "u", "s", "k", head_live=True, live_link_inputs="live-value"
    )
    await run_scrubs(scrubs, apply=True)
    rows = ch_client.query(
        "SELECT path, suggestion FROM loom.memory_code_links WHERE org = {o:String} "
        "AND namespace = {n:String} ORDER BY path",
        parameters={"o": ORG, "n": MEM},
    ).result_rows
    assert rows == [("a.py", ""), ("b.py", "about the live text")]


def _notebook(body: str) -> str:
    import json

    cells = [
        {"cell_type": "markdown", "source": ["# Rows"]},
        {"cell_type": "code", "source": body.splitlines(keepends=True)},
    ]
    return json.dumps({"cells": cells, "nbformat": 4}, indent=1)


async def test_an_edit_to_a_notebook_cell_reads_changed(env) -> None:
    repo = env
    c1 = repo.commit({"nb/rows.ipynb": _notebook("def tally_rows(xs):\n    return len(xs)\n")})
    await _index(TRUNK, repo, c1, ["nb/rows.ipynb"])
    mid = await _remember(MEM, "tally", "`tally_rows` counts the rows")
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["fresh"]
    c2 = repo.commit({"nb/rows.ipynb": _notebook("def tally_rows(xs):\n    return len(xs) + 1\n")})
    await _index(TRUNK, repo, c2, ["nb/rows.ipynb"])
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["changed"]


async def test_a_candidate_elsewhere_that_does_not_parse_leaves_only_its_link_unsure(
    env, monkeypatch
) -> None:
    from server.code.app.index import ingest_parse
    from server.memory.app import code_link_pass, code_link_recheck

    repo = env
    c1 = repo.commit({"src/a.py": "def alpha():\n    return 1\n\n\ndef gamma():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/a.py"])
    alpha = await _remember(MEM, "alpha", "`alpha` returns one")
    gamma = await _remember(MEM, "gamma", "`gamma` returns one")
    await _pass()
    # alpha edited in place; gamma moved to a file the worker cannot parse
    moved = {
        "src/a.py": "def alpha():\n    return 2\n",
        "src/broken.py": "def gamma():\n    return 1\n",
    }
    c2 = repo.commit(moved)
    await _index(TRUNK, repo, c2, list(moved))
    parse = ingest_parse._PARSERS["python"]

    def broken(path: str, source: bytes) -> Any:
        if path.endswith("broken.py"):
            raise RecursionError("boom")
        return parse(path, source)

    monkeypatch.setitem(ingest_parse._PARSERS, "python", broken)
    for _ in range(2):
        result = code_link_pass.PassResult()
        await code_link_recheck.recheck_all(result)
        assert not result.errors, result.errors  # its own file parses: nothing to retry
    assert [link["state"] for link in await _links(alpha)] == ["changed"]
    assert [link["state"] for link in await _links(gamma)] == ["unresolved"]


async def test_a_memory_whose_judgement_fails_goes_behind_the_others(env, monkeypatch) -> None:
    from server.memory.app import code_link_judge, code_link_pass, code_link_recheck
    from server.settings import settings

    repo = env
    src = "def poison_fn():\n    return 1\n\n\ndef fine_fn():\n    return 1\n"
    c1 = repo.commit({"src/m.py": src})
    await _index(TRUNK, repo, c1, ["src/m.py"])
    await _set_policy(MEM, "revise")
    await _remember(MEM, "poison", "`poison_fn` returns one")
    await _remember(MEM, "fine", "`fine_fn` returns one")
    await _pass()
    asked: list[str] = []

    async def judge(claim: str, before: str, after: str) -> tuple[str, str]:
        asked.append(claim)
        if claim == asked[0]:
            raise RuntimeError("provider 400")  # the first memory asked always fails
        return "unsure", ""

    monkeypatch.setattr(code_link_judge, "judge_claim", judge)
    monkeypatch.setattr(settings, "memory_code_links_revise_per_pass", 1)
    c2 = repo.commit({"src/m.py": src.replace("return 1", "return 2")})
    await _index(TRUNK, repo, c2, ["src/m.py"])
    for _ in range(3):
        await code_link_recheck.recheck_all(code_link_pass.PassResult())
        await asyncio.sleep(1.1)  # the failure's stamp is a second later than the rest
    assert len(set(asked)) == 2, asked


async def test_a_lapsed_lock_no_longer_keeps_a_memory_whose_code_is_gone(env) -> None:
    from server.memory.api.models_lock import MemoryLockInput
    from server.memory.app.lock import handle_memory_lock

    await _drop_fetch_rows(env, TRUNK)
    await _remember(MEM, "note", "`fetch_rows` returns n")
    await _pass()
    lock = {"org": ORG, "namespace": MEM, "agent": "claude-code", "memory_key": "note"}
    await handle_memory_lock(MemoryLockInput(**lock, mode="no_delete", reason="r", ttl_seconds=1))
    await asyncio.sleep(2)
    await _remove_fetch_rows(env, TRUNK)
    await _pass()
    assert "note" not in await _search(MEM, "fetch_rows returns n")


async def test_validate_counts_stale_memories_past_the_sample_limit(env) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models_namespace_catalog import NamespaceValidateInput
    from server.identity.app.namespace_validate import handle_namespace_validate
    from server.operations.base import Role

    await _drop_fetch_rows(env, TRUNK)
    # files out of order and past ASCII: the SQL inputs hash must sort them as Python does
    await _remember(MEM, "first", "`fetch_rows` returns n", files=["src/é.py", "src/c.py"])
    await _pass()
    await _remember(MEM, "second", "`fetch_rows` returns rows", files=["src/c.py", "Z.py"])
    await _pass()
    await _remove_fetch_rows(env, TRUNK)
    await _pass()

    async def stale(limit: int) -> Any:
        auth = AuthInfo(
            subject="it@example.com",
            org=ORG,
            namespaces=(MEM, TRUNK),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
        set_current_auth(auth)
        try:
            report = await handle_namespace_validate(
                NamespaceValidateInput(org=ORG, namespace=MEM, sample_limit=limit)
            )
        finally:
            set_current_auth(None)
        return next(f for f in report.findings if f.check == "code_links_stale")

    none = await stale(0)
    assert (none.count, none.samples) == (2, [])
    every = await stale(10)
    assert every.count == 2 and {s["memory_key"] for s in every.samples} == {"first", "second"}
    assert (await stale(1)).samples == every.samples[:1]


async def test_the_recent_tier_carries_code_state_and_leaves_out_gone_code(env) -> None:
    await _drop_fetch_rows(env, TRUNK)
    await _remember(MEM, "gone", "`fetch_rows` returns n")
    await _remember(MEM, "kept", "`other` returns zero")
    await _pass()
    await _remove_fetch_rows(env, TRUNK)
    await _pass()
    served = await _search(MEM, "zebra crossings at noon", recent_first=5)
    assert "gone" not in served and served.get("kept") == "fresh"


async def test_a_file_that_fails_to_fingerprint_wedges_no_other(env, monkeypatch) -> None:
    from server.code.app import memory_links

    files = {
        "a.py": "def tally_rows():\n    return 1\n",
        "b.py": "def count_rows():\n    return 1\n",
    }
    c1 = env.commit(files)
    await _index(TRUNK, env, c1, list(files))
    tally = await _remember(MEM, "tally", "`tally_rows` returns one", files=["a.py"])
    count = await _remember(MEM, "count", "`count_rows` returns one", files=["b.py"])
    await _pass()
    real = memory_links.definitions

    def broken(path: str, *args, **kwargs):
        if path == "a.py":
            raise TypeError("a fingerprint rule failed")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(memory_links, "definitions", broken)
    c2 = env.commit({p: src.replace("1", "2") for p, src in files.items()})
    await _index(TRUNK, env, c2, list(files))
    with pytest.raises(AssertionError, match="did not parse: a.py"):
        await _pass()
    assert [link["state"] for link in await _links(tally)] == ["fresh"]
    assert [link["state"] for link in await _links(count)] == ["changed"]


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "rows.go",
            "package rows\n\ntype tally_rows struct{ n int }\n",
            "package rows\n\ntype tally_rows struct {\n\tn int // count\n}\n",
        ),
        ("rows.py", "tally_rows = dict(a=1,b=2)\n", "tally_rows = dict(a=1, b=2)  # two\n"),
    ],
)
async def test_a_reformatted_type_or_constant_stays_fresh(env, path, before, after) -> None:
    c1 = env.commit({path: before})
    await _index(TRUNK, env, c1, [path])
    mid = await _remember(MEM, "fmt", "`tally_rows` holds the counts", files=[path])
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["fresh"]
    c2 = env.commit({path: after})
    await _index(TRUNK, env, c2, [path])
    await _pass()
    assert [link["state"] for link in await _links(mid)] == ["fresh"]


async def test_stale_fingerprints_page_by_key_each_link_once(env) -> None:
    import uuid

    from server.core.ch import get_client
    from server.memory.store import code_links as store

    cns = "rehash-pages"

    def link(path: str, **kw: Any) -> Any:
        return store.link_row(
            org=ORG,
            code_namespace=cns,
            namespace=MEM,
            memory_id=str(uuid.uuid4()),
            path=path,
            qualified_name=f"{path}::f",
            **{"fingerprint_version": "0-old", **kw},
        )

    stale = [link(p) for p in ("a.py", "b.py", "c.py") for _ in range(3)]
    others = [link("a.py", fingerprint_version="now"), link("", state="pending")]
    await get_client().insert(store.TABLE, [*stale, *others])
    pages: list[list[str]] = []
    after: tuple[str, ...] | None = None
    while True:
        rows, after = await store.stale_fingerprints(ORG, cns, "now", after=after, limit=2)
        pages.append([r["memory_id"] for r in rows])
        if after is None:
            break
    seen = [m for page in pages for m in page]
    assert sorted(seen) == sorted(a["memory_id"] for a in stale) and len(pages) == 5


async def test_only_links_under_these_rules_are_due_for_policy(env) -> None:
    import uuid

    from server.core.ch import get_client
    from server.memory.store import code_links as store

    cns = "due-rules"

    def link(version: str) -> Any:
        return store.link_row(
            org=ORG,
            code_namespace=cns,
            namespace=MEM,
            memory_id=str(uuid.uuid4()),
            path="a.py",
            qualified_name="a::f",
            state="changed",
            policy_due=1,
            fingerprint_version=version,
        )

    now, old = link("now"), link("0-old")
    await get_client().insert(store.TABLE, [now, old])
    assert await store.policy_due(ORG, cns, "now", limit=10) == [now["memory_id"]]


async def test_a_user_erasure_waits_out_a_pass_about_to_publish_its_cursor(
    env, ch_client, monkeypatch
) -> None:
    from server.code.app import memory_links
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app.handlers import handle_erasure_request
    from server.memory.store import code_links as store
    from server.memory.workers.code_links import run_pass
    from server.settings import settings

    repo = env
    user = f"erased-{os.urandom(3).hex()}"
    c1 = repo.commit({"src/u.py": "def user_value():\n    return 1\n"})
    await _index(TRUNK, repo, c1, ["src/u.py"])
    monkeypatch.setattr(settings, "memory_code_links_batch", 1)  # the one write below
    await memory_links.set_link_cursor("queue", json.dumps(list(await store.queue_tail() or [])))
    await _remember(MEM, "u", "`user_value` is one", user_id=user)

    publishing, resume = asyncio.Event(), asyncio.Event()
    write = memory_links.set_link_cursor

    async def paused(name: str, value: str) -> None:
        if name == "queue" and not resume.is_set():
            publishing.set()
            await resume.wait()  # read the user's write; erased before it is published
        await write(name, value)

    monkeypatch.setattr(memory_links, "set_link_cursor", paused)
    worker = asyncio.create_task(run_pass())
    await asyncio.wait_for(publishing.wait(), 30)
    request = ErasureRequestInput(
        org=ORG, scope_type="user_id", scope_value=user, requester="it@example.com"
    )
    erasing = asyncio.create_task(handle_erasure_request(request))
    await asyncio.sleep(2)
    assert not erasing.done()  # it waits for the pass to give up the queue lease
    resume.set()
    await worker
    out = await asyncio.wait_for(erasing, 60)
    cursors = ch_client.query(
        "SELECT state_value FROM loom.code_index_state "
        "WHERE org = '' AND namespace = 'memory_code_links'"
    ).result_rows
    assert out.status == "completed"
    assert not [v for (v,) in cursors if user in v]
