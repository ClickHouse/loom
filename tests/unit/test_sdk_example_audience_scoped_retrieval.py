"""sdk/python/examples/audience_scoped_retrieval.py runs end to end against a scripted
server that keeps collections, grants and documents per signed-in user: the private
request reads the note and both collections, the shared one only the destination's
collection, no tool argument widens it, and what the run made is removed."""

from __future__ import annotations

import functools
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

EXAMPLE = Path(__file__).resolve().parents[2] / "sdk/python/examples/audience_scoped_retrieval.py"


def load_example(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("audience_scoped_retrieval_example", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses resolve their annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class Server:
    """Loom as the example sees it. The bearer token is the user's id (the example is
    given `sign=str`); a read of a collection the user holds no grant on answers 403."""

    def __init__(
        self,
        *,
        searches_everything: bool = False,
        keeps_grants: bool = False,
        keeps_collections: bool = False,
        keeps_notes: bool = False,
        lists_revoked: bool = False,
    ) -> None:
        self.searches_everything = searches_everything
        self.keeps_grants = keeps_grants
        self.keeps_collections = keeps_collections
        self.keeps_notes = keeps_notes
        self.lists_revoked = lists_revoked  # collection.list lags behind a revoke
        self.revoked: set[tuple[str, str]] = set()  # (collection, user)
        self.grants: dict[str, set[str]] = {}  # collection → users who read it
        self.docs: dict[tuple[str, str], str] = {}  # (collection, source_uri) → content
        self.calls: list[tuple[str, str, dict[str, Any]]] = []  # (user, op, body)
        self.deleted: list[str] = []
        self.distances: dict[tuple[str, str], float] = {}  # a document's, when not 0.2
        self.during: dict[str, Any] = {}  # op → called while a call of it is in flight

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(self)),
        )

    def searches(self) -> list[tuple[str, set[str]]]:
        """(user, collections named) of every knowledge.search."""
        return [
            (user, {body["namespace"], *body.get("namespaces", [])})
            for user, op, body in self.calls
            if op == "knowledge.search"
        ]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        op = req.url.path.removeprefix("/v1/")
        body = json.loads(req.content or b"{}")
        user = req.headers["Authorization"].removeprefix("Bearer ")
        own = f"col-me-{user}"
        self.calls.append((user, op, body))
        if op in self.during:
            self.during[op]()

        def name(namespace: str) -> str:
            return own if namespace == "@me" else namespace

        def reads(namespace: str) -> bool:
            return namespace == own or user in self.grants.get(namespace, ())

        ns = name(body.get("namespace", "@me"))
        if op == "collection.list":
            rows = [{"namespace": own, "personal": True, "role": "admin"}]
            rows += [
                {"namespace": c, "personal": False, "role": "viewer"}
                for c, users in sorted(self.grants.items())
                if user in users or (self.lists_revoked and (c, user) in self.revoked)
            ]
            return httpx.Response(200, json={"collections": rows, "next_cursor": None})
        if op == "collection.create":
            self.grants[ns] = {user}
            return httpx.Response(200, json={"namespace": ns, "created": True})
        if not reads(ns):
            return httpx.Response(403, json={"detail": {"code": "AUTH_INSUFFICIENT"}})
        if op == "grants.create":
            self.grants[ns].add(body["subject"].removeprefix("user:"))
        elif op == "grants.revoke" and not self.keeps_grants:
            self.grants[ns].discard(body["subject"].removeprefix("user:"))
            self.revoked.add((ns, body["subject"].removeprefix("user:")))
        elif op == "collection.delete":
            self.deleted.append(ns)
            if self.keeps_collections:
                return httpx.Response(200, json={"deleted": False, "erasure_status": "failed"})
            del self.grants[ns]
            self.revoked = {key for key in self.revoked if key[0] != ns}
            self.docs = {key: text for key, text in self.docs.items() if key[0] != ns}
            return httpx.Response(200, json={"deleted": True, "erasure_status": "completed"})
        elif op == "knowledge.add":
            self.docs[(ns, body["source_uri"])] = body["content"]
        elif op == "knowledge.delete":
            if self.keeps_notes and ns == own:
                return httpx.Response(400, json={"detail": {"code": "VALIDATION_ERROR"}})
            self.docs.pop((ns, body["source_uri"]), None)
            self.deleted.append(ns)
        elif op == "knowledge.get":
            content = self.docs.get((ns, body["source_uri"]))
            return httpx.Response(200, json={"found": content is not None, "content": content})
        elif op == "knowledge.search":
            if len(body.get("namespaces", [])) > 100:  # NamedNamespaces' max_length
                return httpx.Response(422, json={"detail": {"code": "VALIDATION_ERROR"}})
            named = {ns, *map(name, body.get("namespaces", []))}
            if not all(reads(n) for n in named):
                return httpx.Response(403, json={"detail": {"code": "AUTH_INSUFFICIENT"}})
            everything = self.searches_everything or body.get("namespace_scope") == "all_mine"
            hits = [
                {
                    "source_uri": uri,
                    "chunk_idx": 0,
                    "content": content,
                    "tags": [],
                    "distance": self.distances.get((collection, uri), 0.2),
                    "similarity": 0.8,
                    "namespace": collection,
                }
                for (collection, uri), content in self.docs.items()
                if (reads(collection) if everything else collection in named)
            ]
            return httpx.Response(200, json={"results": hits})
        return httpx.Response(200, json={})


async def test_every_check_passes_and_the_run_removes_what_it_made(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = Server()
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    assert await example.main(sign=str) == 0
    out = capsys.readouterr().out
    assert "all checks passed" in out
    private, _, shared = out.partition("shared reply in channel:support may draw on:")
    assert example.NOTE in private and example.FINANCE in private and example.SUPPORT in private
    assert example.SUPPORT in shared
    assert example.NOTE not in shared and example.FINANCE not in shared
    assert not server.docs and not server.grants, "the note and both collections are gone"
    assert len(server.deleted) == 3


async def test_a_shared_request_names_only_the_destinations_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    assert await example.main(sign=str) == 0
    person = next(user for user, _ in server.searches() if user.startswith("ana-"))
    named = [collections for user, collections in server.searches() if user == person]
    # The private request names the personal collection and both shared ones; every
    # later search, in the shared conversation, names the support collection alone.
    assert len(named[0]) == 3 and f"col-me-{person}" in named[0]
    support = {next(c for c in named[0] if c.startswith("col-example-support-"))}
    assert named[1:] and all(collections == support for collections in named[1:])
    # No search carries a field that would widen it or name another user.
    for _, op, body in server.calls:
        if op == "knowledge.search":
            assert not {"namespace_scope", "user_id"} & set(body), body


async def test_a_refused_tool_call_and_an_unknown_destination_send_nothing_to_loom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    host = example.Host(
        "acme", str, {"channel:leads": example.Destination(frozenset({"lead"}), ("col-a",))}
    )
    talk = host.conversation("ana", "channel:leads")
    for name, arguments in (
        ("search", {"query": "q", "namespaces": ["@me"]}),
        ("search", {}),
        ("open_citation", {"handle": "c-1", "source_uri": "docs://x"}),
        ("knowledge.search", {"query": "q"}),
    ):
        with pytest.raises(example.ScopeRefused):
            await host.call_tool(talk, name, arguments)
    assert await host.call_tool(talk, "search", {"query": "q"}) == []  # not a member
    unknown = host.conversation("ana", "channel:unknown")
    assert await host.call_tool(unknown, "search", {"query": "q"}) == []
    assert server.calls == []
    with pytest.raises(example.ScopeRefused):
        await host.deliver(talk, "channel:unknown", "reply", talk.generation)
    for nowhere in (unknown, talk):
        with pytest.raises(example.ScopeRefused, match="not a destination"):
            await host.deliver(nowhere, nowhere.destination, "reply", nowhere.generation)
    assert server.calls == []


async def test_a_hit_from_outside_the_named_collections_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server(searches_everything=True)
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    with pytest.raises(example.ScopeRefused, match="outside the scope"):
        await example.main(sign=str)
    assert not server.docs and not server.grants, "cleanup still ran"


async def test_a_grant_that_stays_after_the_revoke_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = Server(keeps_grants=True)
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    monkeypatch.setattr(example, "REVOKE_WAIT_S", 0)
    assert await example.main(sign=str) == 1
    out = capsys.readouterr().out
    assert "still came back after the revoke" in out
    assert "a citation opened after the revoke" in out
    assert "a reply drafted before the revoke was delivered" in out
    assert "3 check(s) failed" in out


async def test_a_collection_that_is_not_deleted_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = Server(keeps_collections=True)
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    assert await example.main(sign=str) == 1
    out = capsys.readouterr().out
    assert out.count("was not deleted") == 2 and "2 check(s) failed" in out


@pytest.mark.parametrize("sources_after", [(), ("col-a",)])
async def test_a_reply_is_dropped_when_its_destination_gains_a_reader(
    monkeypatch: pytest.MonkeyPatch, sources_after: tuple[str, ...]
) -> None:
    server = Server()
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    server.grants["col-a"] = {"ana", "lead"}
    server.docs[("col-a", "docs://a")] = "shared text"
    host = example.Host(
        "acme", str, {"channel:a": example.Destination(frozenset({"ana", "lead"}), ("col-a",))}
    )
    talk = host.conversation("ana", "channel:a")
    assert len(await host.call_tool(talk, "search", {"query": "q"})) == 1
    drafted = talk.generation
    assert await host.deliver(talk, "channel:a", "reply", drafted) == "reply"

    host.destinations["channel:a"] = example.Destination(
        frozenset({"ana", "lead", "guest"}), sources_after
    )
    with pytest.raises(example.ScopeRefused, match="changed its readers or sources"):
        await host.deliver(talk, "channel:a", "reply", drafted)
    assert not talk.passages, "the context the reply was drafted from is gone"
    assert talk.readers is None
    # The same draft is refused again, though nothing is left to compare it with.
    with pytest.raises(example.ScopeRefused, match="was discarded"):
        await host.deliver(talk, "channel:a", "reply", drafted)
    # The next request starts over, for the readers the destination has now.
    again = await host.call_tool(talk, "search", {"query": "q"})
    assert len(again) == len(sources_after)
    with pytest.raises(example.ScopeRefused, match="was discarded"):
        await host.deliver(talk, "channel:a", "reply", drafted)
    assert await host.deliver(talk, "channel:a", "a new reply", talk.generation) == "a new reply"


async def test_a_draft_is_refused_on_every_retry_after_its_source_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    server.grants["col-a"] = {"ana", "lead"}
    server.docs[("col-a", "docs://a")] = "shared text"
    host = example.Host(
        "acme", str, {"channel:a": example.Destination(frozenset({"ana", "lead"}), ("col-a",))}
    )
    talk = host.conversation("ana", "channel:a")
    assert len(await host.call_tool(talk, "search", {"query": "q"})) == 1
    drafted = talk.generation
    host.destinations["channel:a"] = example.Destination(frozenset({"ana", "lead"}))
    with pytest.raises(example.ScopeRefused, match="changed its readers or sources"):
        await host.deliver(talk, "channel:a", "reply", drafted)
    for _ in range(2):
        with pytest.raises(example.ScopeRefused, match="was discarded"):
            await host.deliver(talk, "channel:a", "reply", drafted)


@pytest.mark.parametrize("tool", ["search", "open_citation", "none before delivery"])
async def test_a_read_loom_refuses_in_a_collection_still_listed_drops_the_draft(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    server = Server(lists_revoked=True)
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    server.grants["col-a"] = {"ana", "lead"}
    server.docs[("col-a", "docs://a")] = "shared text"
    host = example.Host(
        "acme", str, {"channel:a": example.Destination(frozenset({"ana", "lead"}), ("col-a",))}
    )
    talk = host.conversation("ana", "channel:a")
    (passage,) = await host.call_tool(talk, "search", {"query": "q"})
    drafted = talk.generation
    server.grants["col-a"].discard("ana")
    server.revoked.add(("col-a", "ana"))
    assert await host.scope("ana", "channel:a") == ["col-a"], "the listing still names it"
    # Loom refuses the read; the host turns that into its own answer.
    if tool == "search":
        assert await host.call_tool(talk, "search", {"query": "q"}) == []
    elif tool == "open_citation":
        with pytest.raises(example.ScopeRefused, match="no longer in scope"):
            await host.call_tool(talk, "open_citation", {"handle": passage["handle"]})
    else:  # delivery comes straight after the revoke, and makes the refused read itself
        with pytest.raises(example.ScopeRefused, match="lost a source"):
            await host.deliver(talk, "channel:a", "reply", drafted)
    assert not talk.passages, "the context the reply was drafted from is gone"
    # The listing would still pass the draft, so the refused read is what drops it.
    with pytest.raises(example.ScopeRefused, match="was discarded"):
        await host.deliver(talk, "channel:a", "reply", drafted)


async def test_every_check_passes_while_the_listing_lags_behind_the_revoke(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = Server(lists_revoked=True)
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    assert await example.main(sign=str) == 0
    assert "all checks passed" in capsys.readouterr().out


async def test_a_scope_wider_than_one_search_takes_is_searched_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    collections = [f"col-{n:03}" for n in range(101)]  # with the personal one: 102
    server.grants.update({collection: {"ana"} for collection in collections})
    server.docs[("col-me-ana", "docs://note")] = "my note"
    server.docs[(collections[-1], "docs://last")] = "the last collection"
    host = example.Host(
        "acme", str, {"dm:ana": example.Destination(frozenset({"ana"}), private=True)}
    )
    found = await host.call_tool(host.conversation("ana", "dm:ana"), "search", {"query": "q"})
    assert {p["content"] for p in found} == {"my note", "the last collection"}
    named = [collections for _, collections in server.searches()]
    assert [len(batch) for batch in named] == [101, 1]
    assert set().union(*named) == {"col-me-ana", *collections}

    # Across batches the nearest passages win, whichever batch they came in.
    monkeypatch.setattr(example, "TOP_K", 1)
    server.distances[("col-me-ana", "docs://note")] = 0.1
    found = await host.call_tool(host.conversation("ana", "dm:ana"), "search", {"query": "q"})
    assert [p["content"] for p in found] == ["my note"]


@pytest.mark.parametrize("op", ["collection.list", "knowledge.get"])
@pytest.mark.parametrize(
    ("members", "sources"), [({"ana", "lead", "guest"}, ("col-a",)), ({"ana", "lead"}, ())]
)
async def test_a_record_changed_while_delivery_asks_loom_stops_the_reply(
    monkeypatch: pytest.MonkeyPatch, op: str, members: set[str], sources: tuple[str, ...]
) -> None:
    server = Server()
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    server.grants["col-a"] = {"ana", "lead"}
    server.docs[("col-a", "docs://a")] = "shared text"
    host = example.Host(
        "acme", str, {"channel:a": example.Destination(frozenset({"ana", "lead"}), ("col-a",))}
    )
    talk = host.conversation("ana", "channel:a")
    assert len(await host.call_tool(talk, "search", {"query": "q"})) == 1
    changed = example.Destination(frozenset(members), sources)
    server.during[op] = lambda: host.destinations.update({"channel:a": changed})
    with pytest.raises(example.ScopeRefused, match="changed its readers or sources"):
        await host.deliver(talk, "channel:a", "reply", talk.generation)


async def test_a_grant_revoked_while_delivery_lists_collections_stops_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server(lists_revoked=True)
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    server.grants["col-a"] = {"ana", "lead"}
    server.docs[("col-a", "docs://a")] = "shared text"
    host = example.Host(
        "acme", str, {"channel:a": example.Destination(frozenset({"ana", "lead"}), ("col-a",))}
    )
    talk = host.conversation("ana", "channel:a")
    assert len(await host.call_tool(talk, "search", {"query": "q"})) == 1

    def revoke() -> None:
        server.grants["col-a"].discard("ana")
        server.revoked.add(("col-a", "ana"))

    # The listing still names the collection, so the read has to come after it.
    server.during["collection.list"] = revoke
    with pytest.raises(example.ScopeRefused, match="lost a source"):
        await host.deliver(talk, "channel:a", "reply", talk.generation)


async def test_a_note_that_is_not_deleted_still_lets_both_collections_go(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = Server(keeps_notes=True)
    server.install(monkeypatch)
    example = load_example(monkeypatch)
    assert await example.main(sign=str) == 1
    out = capsys.readouterr().out
    assert "the note was not deleted" in out and "1 check(s) failed" in out
    assert not server.grants, "both collections were still deleted"
