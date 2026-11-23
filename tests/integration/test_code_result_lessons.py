"""The lessons a code answer carries, read from live ClickHouse: rows written through
memory.set and matched to a result's file by its path, never by another directory's file
of the same name, a decision served whole."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

PATH = "server/code/app/search/handler.py"
DECISION = "Decided: the search memo never stores a caller's memories, " * 6


def _set(
    org: str,
    base: str,
    token: str,
    key: str,
    value: str,
    files: list[str],
    **kw: Any,
) -> None:
    body = {"org": org, "namespace": "dev", "agent": "claude-code", "memory_key": key}
    body |= {"value": value, "files": files, **kw}
    r = httpx.post(
        f"{base}/v1/memory.set", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    assert r.status_code == 200, r.text


async def test_grep_attaches_requested_file_lessons_and_respects_opt_out(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    from server.settings import settings

    org = unique_org("grep-lessons")
    writer = mint_test_token(org=org, namespaces=["*"], roles=["writer"])
    _set(org, control_plane_url, writer, "notes/allowed", "Keep callback order.", [PATH])
    ch_client.insert(
        "loom.code_chunks",
        [
            [
                org,
                "code",
                "m",
                PATH,
                uuid.uuid4(),
                "function",
                "run",
                1,
                2,
                "def run():\n    return callback()\n",
                [0.0] * settings.embedding_dim,
                1,
            ]
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
    reader = mint_test_token(org=org, namespaces=["code", "dev"], roles=["viewer"])
    body = dict(org=org, namespace="code", pattern="callback", lesson_namespaces=["dev"])
    async with httpx.AsyncClient(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {reader}"}, timeout=10
    ) as client:
        response = await client.post("/v1/code.grep", json=body)
        assert response.status_code == 200, response.text
        payload = response.json()
        data = payload.get("data", payload)
        assert data["matches"][0]["path"] == PATH
        assert {row["memory_key"] for row in data["lessons"]} == {"notes/allowed"}
        response = await client.post("/v1/code.grep", json={**body, "include_lessons": False})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert "lessons" not in payload.get("data", payload)


async def test_enclosing_attaches_only_authorized_resolved_file_lessons(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    org = unique_org("enclosing-lessons")
    writer = mint_test_token(org=org, namespaces=["*"], roles=["writer"])
    _set(org, control_plane_url, writer, "notes/allowed", "Keep run callback order.", [PATH])
    _set(org, control_plane_url, writer, "notes/unrelated", "Storage configuration.", [PATH])
    _set(org, control_plane_url, writer, "notes/missing", "Unresolved file note.", ["absent.py"])
    ch_client.insert(
        "loom.code_symbols",
        [[org, "code", "treesitter", "pkg::run", "run-id", "run", "function", PATH, 1, 5]],
        column_names=[
            "org",
            "namespace",
            "tier",
            "qualified_name",
            "symbol_id",
            "name",
            "kind",
            "path",
            "start_line",
            "end_line",
        ],
    )
    body = dict(
        org=org,
        namespace="code",
        locations=[f"{PATH}:2", f"{PATH}:3", "absent.py:1"],
        lesson_namespaces=["dev"],
    )
    for names, include in [(["code", "dev"], True), (["code", "dev"], False), (["code"], True)]:
        reader = mint_test_token(org=org, namespaces=names, roles=["viewer"])
        async with httpx.AsyncClient(
            base_url=control_plane_url, headers={"Authorization": f"Bearer {reader}"}, timeout=10
        ) as client:
            response = await client.post(
                "/v1/code.enclosing", json={**body, "include_lessons": include}
            )
        assert response.status_code == 200, response.text
        payload = response.json()
        data = payload.get("data", payload)
        assert data["resolved"] == 2 and len(data["results"]) == 3
        if include and "dev" in names:
            assert data["lessons"][0]["memory_key"] == "notes/allowed"
            assert {row["memory_key"] for row in data["lessons"]} == {
                "notes/allowed",
                "notes/unrelated",
            }
            assert data["lessons"][0]["path"] == PATH
        else:
            assert "lessons" not in data


async def test_lessons_match_the_result_file_by_path(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    from server.core.ch import get_client
    from server.memory.app import path_lessons

    org = unique_org("code-result-lessons")
    await get_client().connect()
    token = mint_test_token(org=org, namespaces=["*"], roles=["writer"])
    _set(org, control_plane_url, token, "notes/exact", "Pages are cut by tokens.", [PATH])
    _set(
        org,
        control_plane_url,
        token,
        "notes/bare",
        "Every handler logs its route.",
        ["handler.py"],
    )
    _set(
        org,
        control_plane_url,
        token,
        "notes/other",
        "The memory handler drops weak hits.",
        ["server/memory/app/loom_search/handler.py"],
    )
    _set(org, control_plane_url, token, "design/memo", DECISION, [PATH], memory_type="decision")
    long_note = (
        "Callbacks release their context resources. " * 60
        + "Source: https://example.com/design#cleanup"
    )
    _set(org, control_plane_url, token, "notes/long", long_note, [PATH])
    # More rows about other directories' handler.py than a small read would keep: the
    # exact path's rows still come back.
    for i in range(12):
        _set(
            org,
            control_plane_url,
            token,
            f"notes/elsewhere-{i}",
            f"Handler {i} retries twice.",
            [f"server/plane{i}/app/handler.py"],
            confidence=1.0,
        )

    rows: list[dict[str, Any]] = []
    for _ in range(50):
        rows = (
            await path_lessons.lessons_for_paths(org, ["dev"], "", [PATH], token_budget=2000)
        ).rows
        if len(rows) >= 4:
            break
        await asyncio.sleep(0.2)
    keys = {r["memory_key"] for r in rows}
    assert keys == {"notes/exact", "notes/bare", "design/memo", "notes/long"}, rows
    by_key = {r["memory_key"]: r for r in rows}
    assert by_key["notes/exact"] == {
        "path": PATH,
        "memory_key": "notes/exact",
        "namespace": "dev",
        "excerpt": "Pages are cut by tokens.",
        "code_state": "",
    }
    assert by_key["design/memo"]["excerpt"] == " ".join(DECISION.split())
    assert by_key["notes/long"]["excerpt"] == long_note
    # Symbol lessons use the same stored value through their own read projection.
    from server.memory.store.code_plane_reads import served_excerpts

    response = await asyncio.to_thread(
        httpx.post,
        f"{control_plane_url}/v1/memory.get",
        json={"org": org, "namespace": "dev", "memory_key": "notes/long"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    memory_id = payload.get("data", payload)["memory_id"]
    (stored,) = await served_excerpts(org, ["dev"], [memory_id])
    assert stored["excerpt"] == long_note
    # a cap below the decision's size leaves it out, never cut
    small = (await path_lessons.lessons_for_paths(org, ["dev"], "", [PATH], token_budget=60)).rows
    assert "design/memo" not in {r["memory_key"] for r in small}


@pytest.mark.slow
async def test_a_search_keeps_a_stored_lesson_when_another_file_read_times_out(
    control_plane_url: str, mint_test_token: Any, ch_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
    from server.code.app import result_lessons
    from server.code.domain.units import CodeUnit
    from server.core.ch import get_client
    from server.memory.app import path_lessons
    from server.settings import settings

    org = unique_org("code-result-lessons")
    await get_client().connect()
    token = mint_test_token(org=org, namespaces=["*"], roles=["writer"])
    _set(org, control_plane_url, token, "notes/ready", "Keep the ready file's lesson.", [PATH])
    listing_rows = path_lessons.listing_rows
    stopped = asyncio.Event()

    async def _rows(inp: Any) -> Any:
        if inp.path == PATH:
            return await listing_rows(inp)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def _search(inp: CodeSearchInput) -> CodeSearchOutput:
        return CodeSearchOutput(
            results=[CodeUnit(path=PATH), CodeUnit(path="unavailable.py")], route="hybrid"
        )

    monkeypatch.setattr(path_lessons, "listing_rows", _rows)
    monkeypatch.setattr(settings, "code_enrich_timeout_s", 1.0)
    wrapped = result_lessons.WithLessons(_search, result_lessons.search_paths)
    out = await wrapped(
        CodeSearchInput(
            org=org, namespace="code-repo", query="ready file", lesson_namespaces=["dev"]
        )
    )
    assert any(row["memory_key"] == "notes/ready" for row in out.lessons), out.model_dump()
    assert stopped.is_set()
