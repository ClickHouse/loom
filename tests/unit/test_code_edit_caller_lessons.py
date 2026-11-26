"""Edit lookups attach lessons for the forwarding source they return."""

import time
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from server.code.api.models_symbol import CodeEditContextInput
from server.code.app import enrich, memory_links
from server.code.app.symbol import edit_lessons
from server.code.domain.anchor import Definition
from server.code.domain.units import CodeUnit
from server.memory.app import code_link_reads, code_plane_reads
from server.settings import settings

REVISION = "a" * 40
NOTE = {"memory_key": "mounting", "namespace": "history", "excerpt": "Mounts can repeat."}
FRESH = {**NOTE, "code_state": "fresh"}


def _unit(symbol: str, *, verified: bool = True) -> CodeUnit:
    via: dict[str, Any] = {"path": "web/app.py", "symbol": symbol, "line": 2}
    if verified:
        via["context"] = {
            "start_line": 1,
            "end_line": 2,
            "commit": REVISION,
            "text": "def mount(self):\n    self.register()\n",
        }
    return CodeUnit(
        path="web/receiver.py",
        symbol="Receiver.register",
        examples=[{"evidence": "indirect_call", "via": via}],
    )


@pytest.mark.asyncio
async def test_verified_callers_share_lesson_scope_and_keep_one_copy(monkeypatch):
    definitions = [_unit("App.mount"), _unit("App.mount"), _unit("App.other")]
    originals = [u.model_dump()["examples"] for u in definitions]
    primary = {**FRESH, "memory_key": "direct"}
    different = {**primary, "excerpt": "Mounts need distinct endpoint names."}
    definitions[2].lessons = [primary]
    read = AsyncMock(
        return_value=[
            FRESH,
            primary,
            different,
            {**NOTE, "memory_key": "old", "code_state": "changed"},
        ]
    )
    monkeypatch.setattr(enrich, "lessons_for_symbol", read)
    monkeypatch.setattr(settings, "code_result_lessons_tokens", 0)
    request = CodeEditContextInput(lesson_namespaces=["history"])

    await edit_lessons.attach_verified_caller_lessons(
        request, "org", "repo", definitions, started=time.monotonic()
    )

    assert [call.kwargs["symbol_name"] for call in read.await_args_list] == [
        "App.mount",
        "App.other",
    ]
    for args, kwargs in read.await_args_list:
        assert args == ("org", "repo")
        assert kwargs["fresh_at_commit"] == REVISION
        assert kwargs["extra_namespaces"] == ["history"]
        assert kwargs["paths"] == ["web/app.py"]
        assert 0 < kwargs["seconds"] <= settings.code_enrich_timeout_s
    assert [d.lessons for d in definitions] == [
        [
            {**FRESH, "linked_symbols": ["App.mount"]},
            {**different, "linked_symbols": ["App.mount"]},
        ],
        [],
        [primary],
    ]
    assert [u.examples for u in definitions] == originals


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["disabled", "narrow", "metadata", "expired", "budget"])
async def test_no_caller_lesson_without_scope_source_or_budget(monkeypatch, case):
    read = AsyncMock(return_value=[FRESH])
    monkeypatch.setattr(enrich, "lessons_for_symbol", read)
    monkeypatch.setattr(edit_lessons, "request_narrowed", lambda: case == "narrow")
    monkeypatch.setattr(settings, "code_result_lessons_tokens", 1 if case == "budget" else 0)
    definition = _unit("App.mount", verified=case != "metadata")
    request = CodeEditContextInput(include_lessons=case != "disabled")
    started = time.monotonic() - (settings.code_enrich_timeout_s if case == "expired" else 0)

    await edit_lessons.attach_verified_caller_lessons(
        request, "org", "repo", [definition], started=started
    )

    assert not definition.lessons
    assert read.await_count == (1 if case == "budget" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("head", [REVISION, "b" * 40])
async def test_verified_lesson_lookup_requires_its_index_revision_and_source(monkeypatch, head):
    monkeypatch.setattr(
        code_plane_reads.links_store,
        "for_paths",
        AsyncMock(
            return_value=[
                {"state": "fresh", "qualified_name": "App.mount", "anchored_name": "App.mount"},
            ]
        ),
    )
    monkeypatch.setattr(memory_links, "indexed_head", AsyncMock(return_value=head))
    monkeypatch.setattr(memory_links, "trunk_namespace", lambda *_: "repo")
    monkeypatch.setattr(
        code_link_reads, "current_links", AsyncMock(return_value=[{"state": "fresh"}])
    )
    parsed = AsyncMock(return_value=None)
    monkeypatch.setattr(code_plane_reads, "_definitions_at", parsed)
    older = Mock()
    monkeypatch.setattr(code_plane_reads, "_as_trunk_has_it", older)

    assert (
        await code_plane_reads.lessons_for_symbol(
            "org",
            "repo",
            qualified_name="App.mount",
            paths=["web/app.py"],
            namespaces=["history"],
            fresh_at_commit=REVISION,
        )
        == []
    )

    assert parsed.await_count == int(head == REVISION)
    older.assert_not_called()


@pytest.mark.asyncio
async def test_verified_lesson_only_ranks_fresh_rows(monkeypatch):
    states = ["moved", "moved", "moved", "fresh", "changed"]
    links = [
        {
            "namespace": "history",
            "agent": "writer",
            "user_id": "-",
            "session_id": "-",
            "memory_key": str(key),
            "memory_id": str(key),
            "qualified_name": "App.mount",
            "state": state,
            "changed_since": "b" * 40,
        }
        for key, state in enumerate(states)
    ]
    monkeypatch.setattr(code_plane_reads.links_store, "for_paths", AsyncMock(return_value=links))
    monkeypatch.setattr(memory_links, "indexed_head", AsyncMock(return_value=REVISION))
    monkeypatch.setattr(memory_links, "trunk_namespace", lambda *_: "repo")
    monkeypatch.setattr(code_link_reads, "current_links", AsyncMock(return_value=links))
    monkeypatch.setattr(code_plane_reads, "_definitions_at", AsyncMock(return_value={}))
    monkeypatch.setattr(
        code_plane_reads,
        "_matched",
        lambda *_args, **_kw: [(row, row["state"], "") for row in links],
    )

    by_id = {row["memory_id"]: row for row in links}

    async def excerpts(_org, _namespaces, memories, *, limit, changed):
        matching = sorted(memories, key=lambda identity: identity[-1] in changed)
        return [{**by_id[mid], "mid": mid, "excerpt": mid} for *_, mid in matching[:limit]]

    monkeypatch.setattr(code_plane_reads.reads, "served_excerpts", excerpts)
    monkeypatch.setattr(settings, "code_result_lessons_tokens", 0)
    scope = {"qualified_name": "App.mount", "paths": ["web/app.py"], "namespaces": ["history"]}
    verified = await code_plane_reads.lessons_for_symbol(
        "org", "repo", **scope, fresh_at_commit=REVISION
    )
    ordinary = await code_plane_reads.lessons_for_symbol("org", "repo", **scope, limit=len(states))
    assert [row["code_state"] for row in verified] == ["fresh"]
    assert [row["code_state"] for row in ordinary] == states


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recovered", "stored", "checked", "state", "legacy", "ordinary", "confirmed"),
    [
        ("new", "original", "original", "fresh", True, "fresh", False),
        ("", "original", "original", "fresh", False, "fresh", False),
        ("new", "", "", "fresh", False, "fresh", False),
        ("new", "original", "original", "fresh", False, "changed", False),
        ("original", "original", "original", "fresh", False, "fresh", True),
        ("new", "original", "new", "missing", False, "fresh", False),
        ("new", "original", "new", "unresolved", False, "fresh", False),
        ("original", "original", "new", "missing", False, "fresh", True),
    ],
)
async def test_verified_caller_requires_matching_fingerprint(
    monkeypatch, recovered, stored, checked, state, legacy, ordinary, confirmed
):
    path = "web/app.py"
    symbol = "web.app::App::mount"
    link = {
        "namespace": "history",
        "agent": "writer",
        "user_id": "-",
        "session_id": "-",
        "memory_key": NOTE["memory_key"],
        "memory_id": "one",
        "path": path,
        "anchored_path": path,
        "qualified_name": symbol,
        "anchored_name": symbol,
        "state": state,
        "carried": False,
        "checked_hash": checked,
        "body_hash": stored,
        "anchored_hash": stored,
        "fingerprint_version": "older" if legacy else code_plane_reads.fingerprint_version(),
        "changed_since": REVISION,
        "suggestion": "",
    }
    definition = Definition(path, symbol, "mount", body_hash=recovered)
    monkeypatch.setattr(code_plane_reads.links_store, "for_paths", AsyncMock(return_value=[link]))
    monkeypatch.setattr(memory_links, "indexed_head", AsyncMock(return_value=REVISION))
    monkeypatch.setattr(memory_links, "trunk_namespace", lambda *_: "repo")
    monkeypatch.setattr(code_link_reads, "current_links", AsyncMock(return_value=[link]))
    monkeypatch.setattr(
        code_plane_reads, "_definitions_at", AsyncMock(return_value={(path, symbol): definition})
    )
    monkeypatch.setattr(
        code_plane_reads.reads, "served_excerpts", AsyncMock(return_value=[{**NOTE, "mid": "one"}])
    )
    monkeypatch.setattr(settings, "code_result_lessons_tokens", 0)

    scope = {"qualified_name": symbol, "paths": [path], "namespaces": ["history"]}
    verified = await code_plane_reads.lessons_for_symbol(
        "org", "repo", **scope, fresh_at_commit=REVISION
    )
    direct = await code_plane_reads.lessons_for_symbol("org", "repo", **scope)
    assert [row["code_state"] for row in direct] == [ordinary]
    assert [row["code_state"] for row in verified] == (["fresh"] if confirmed else [])
