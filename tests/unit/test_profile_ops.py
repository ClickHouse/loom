"""Unit tests for the profile ops: CAS-retry, input shape, activity."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from server.memory.api.models_profile import (
    DEFAULT_CHAR_LIMIT,
    ProfileActivityInput,
    ProfileAppendInput,
    ProfileReplaceInput,
)
from server.memory.app.profile import (
    _canonical_json_str,
    handle_profile_activity,
    handle_profile_append,
    handle_profile_replace,
)


def _fake_memory_get_output(*, value: str, char_limit: int | None, sha: str):
    class _R:
        found = True

        def __init__(self) -> None:
            self.value = value
            self.char_limit = char_limit
            self.content_sha256 = sha

    return _R()


def _fake_memory_set_output(*, version: int = 1, memory_id: str = "m1", sha: str = "abc"):
    class _R:
        def __init__(self) -> None:
            self.version = version
            self.memory_id = memory_id
            self.content_sha256 = sha
            self.namespace = "ns1"

    return _R()


def test_canonical_json_str_wraps_strings_in_quotes() -> None:
    assert _canonical_json_str("hello") == '"hello"'
    assert len(_canonical_json_str("hello")) == 7


def test_canonical_json_str_escapes_quotes() -> None:
    assert _canonical_json_str('say "hi"') == '"say \\"hi\\""'


@pytest.mark.asyncio
async def test_profile_append_retries_on_409() -> None:
    """Two 409s then success → returns the third call's result."""
    inp = ProfileAppendInput(org="o", namespace="n", agent="a", label="persona", addition="more")
    get_mock = AsyncMock(
        return_value=_fake_memory_get_output(value="seed", char_limit=2048, sha="sha1")
    )
    set_mock = AsyncMock(
        side_effect=[
            HTTPException(status_code=409, detail={"code": "PRECONDITION_FAILED"}),
            HTTPException(status_code=409, detail={"code": "PRECONDITION_FAILED"}),
            _fake_memory_set_output(version=42, memory_id="m1", sha="sha2"),
        ]
    )
    with (
        patch("server.memory.app.handlers.handle_memory_get", get_mock),
        patch("server.memory.app.write.handle_memory_set", set_mock),
    ):
        out = await handle_profile_append(inp)

    assert out.version == 42
    assert set_mock.await_count == 3


@pytest.mark.asyncio
async def test_profile_append_exhausts_retries() -> None:
    """Three 409s → raises the last 409."""
    inp = ProfileAppendInput(org="o", namespace="n", agent="a", label="persona", addition="more")
    get_mock = AsyncMock(
        return_value=_fake_memory_get_output(value="seed", char_limit=2048, sha="sha1")
    )
    set_mock = AsyncMock(
        side_effect=HTTPException(status_code=409, detail={"code": "PRECONDITION_FAILED"})
    )
    with (
        patch("server.memory.app.handlers.handle_memory_get", get_mock),
        patch("server.memory.app.write.handle_memory_set", set_mock),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await handle_profile_append(inp)
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_profile_append_does_not_retry_on_422() -> None:
    """422 is a caller bug → fail fast, no retry."""
    inp = ProfileAppendInput(
        org="o", namespace="n", agent="a", label="persona", addition="x" * 5000
    )
    get_mock = AsyncMock(
        return_value=_fake_memory_get_output(value="seed", char_limit=10, sha="sha1")
    )
    set_mock = AsyncMock(
        side_effect=HTTPException(status_code=422, detail={"code": "CHAR_LIMIT_EXCEEDED"})
    )
    with (
        patch("server.memory.app.handlers.handle_memory_get", get_mock),
        patch("server.memory.app.write.handle_memory_set", set_mock),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await handle_profile_append(inp)
    assert excinfo.value.status_code == 422
    assert set_mock.await_count == 1


@pytest.mark.asyncio
async def test_profile_append_missing_block_treats_as_set() -> None:
    """Appending to a non-existent section creates it with default cap."""
    inp = ProfileAppendInput(org="o", namespace="n", agent="a", label="fresh", addition="first")

    class _NotFound:
        found = False

    get_mock = AsyncMock(return_value=_NotFound())
    set_mock = AsyncMock(return_value=_fake_memory_set_output(version=7))
    with (
        patch("server.memory.app.handlers.handle_memory_get", get_mock),
        patch("server.memory.app.write.handle_memory_set", set_mock),
    ):
        out = await handle_profile_append(inp)

    assert out.version == 7
    set_call = set_mock.await_args
    written = set_call.args[0]
    assert written.value == "first"
    assert written.char_limit == DEFAULT_CHAR_LIMIT


@pytest.mark.asyncio
async def test_profile_replace_refuses_missing_substring() -> None:
    """`old` not in the section → 422 SUBSTRING_NOT_FOUND."""
    inp = ProfileReplaceInput(org="o", namespace="n", agent="a", label="x", old="absent", new="y")
    get_mock = AsyncMock(
        return_value=_fake_memory_get_output(value="seed value", char_limit=128, sha="sha1")
    )
    set_mock = AsyncMock()
    with (
        patch("server.memory.app.handlers.handle_memory_get", get_mock),
        patch("server.memory.app.write.handle_memory_set", set_mock),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await handle_profile_replace(inp)
    assert excinfo.value.status_code == 422
    assert "SUBSTRING_NOT_FOUND" in str(excinfo.value.detail)
    set_mock.assert_not_called()


@pytest.mark.asyncio
async def test_profile_replace_missing_block_returns_404() -> None:
    """Replacing a non-existent section → 404, not create-as-set."""
    inp = ProfileReplaceInput(org="o", namespace="n", agent="a", label="ghost", old="x", new="y")

    class _NotFound:
        found = False

    get_mock = AsyncMock(return_value=_NotFound())
    with patch("server.memory.app.handlers.handle_memory_get", get_mock):
        with pytest.raises(HTTPException) as excinfo:
            await handle_profile_replace(inp)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_profile_append_returns_value_length() -> None:
    """value_length echoes the post-write canonical-json length."""
    inp = ProfileAppendInput(org="o", namespace="n", agent="a", label="x", addition="b")
    get_mock = AsyncMock(return_value=_fake_memory_get_output(value="a", char_limit=128, sha="s"))
    set_mock = AsyncMock(return_value=_fake_memory_set_output())
    with (
        patch("server.memory.app.handlers.handle_memory_get", get_mock),
        patch("server.memory.app.write.handle_memory_set", set_mock),
    ):
        out = await handle_profile_append(inp)
    assert out.value_length == len(json.dumps("a\nb"))


@pytest.mark.asyncio
async def test_profile_activity_aggregates_per_agent() -> None:
    """CH rows map to entries 1:1, preserving SQL ordering."""
    import datetime as _dt

    inp = ProfileActivityInput(org="o", namespace="n", agent="*", lookback_days=7)
    rows = [
        {
            "agent": "claude-desktop",
            "last_ts": _dt.datetime(2026, 5, 30, 6, 24, 32),
            "n": 3,
            "ops": ["mcp.initialize", "loom_activate"],
        },
        {
            "agent": "cursor",
            "last_ts": _dt.datetime(2026, 5, 28, 14, 0, 0),
            "n": 12,
        },
    ]

    class _FakeCH:
        async def query_all(self, _sql: str, params: dict) -> list[dict]:
            assert params["org"] == "o"
            assert params["ns"] == "n"
            assert params["days"] == 7
            # Every profile-carrying op counts as a read.
            assert "operation IN ({ops:Array(String)})" in _sql
            assert set(params["ops"]) == {
                "loom_activate",
                "profile.render",
                "profile.structured",
                "context.pack",
                "mcp.initialize",
            }
            return rows

    with patch("server.core.ch.get_client", return_value=_FakeCH()):
        out = await handle_profile_activity(inp)

    assert len(out.entries) == 2
    assert out.entries[0].agent == "claude-desktop"
    assert out.entries[0].operations == ["loom_activate", "mcp.initialize"]
    assert out.entries[1].operations == []
    assert out.entries[0].event_count == 3
    assert out.entries[0].last_activated_at == "2026-05-30T06:24:32"
    assert out.entries[1].agent == "cursor"
    assert out.entries[1].event_count == 12


@pytest.mark.asyncio
async def test_profile_activity_clamps_lookback_to_30_days() -> None:
    """Lookback over 30 is clamped server-side."""
    inp = ProfileActivityInput(org="o", namespace="n", agent="*", lookback_days=365)

    captured: dict = {}

    class _FakeCH:
        async def query_all(self, _sql: str, params: dict) -> list[dict]:
            captured.update(params)
            return []

    with patch("server.core.ch.get_client", return_value=_FakeCH()):
        await handle_profile_activity(inp)

    assert captured["days"] == 30


@pytest.mark.asyncio
async def test_profile_activity_clamps_zero_lookback_to_one_day() -> None:
    """A 0-day or negative lookback clamps up to 1."""
    inp = ProfileActivityInput(org="o", namespace="n", agent="*", lookback_days=0)

    captured: dict = {}

    class _FakeCH:
        async def query_all(self, _sql: str, params: dict) -> list[dict]:
            captured.update(params)
            return []

    with patch("server.core.ch.get_client", return_value=_FakeCH()):
        await handle_profile_activity(inp)

    assert captured["days"] == 1


@pytest.mark.asyncio
async def test_profile_activity_503_on_ch_failure() -> None:
    """agent_events unavailable → 503, not 500."""
    inp = ProfileActivityInput(org="o", namespace="n", agent="*")

    class _BrokenCH:
        async def query_all(self, *_a, **_kw) -> list[dict]:
            raise RuntimeError("Code: 60. table loom.agent_events (ch-internal-7:9440)")

    with patch("server.core.ch.get_client", return_value=_BrokenCH()):
        with pytest.raises(HTTPException) as excinfo:
            await handle_profile_activity(inp)
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == {
        "code": "ACTIVITY_UNAVAILABLE",
        "message": "the agent_events read failed (RuntimeError); retry",
    }


@pytest.mark.asyncio
async def test_profile_activity_empty_result_returns_empty_entries() -> None:
    """Zero clients → entries=[] (not None, not 404)."""
    inp = ProfileActivityInput(org="o", namespace="n", agent="*")

    class _EmptyCH:
        async def query_all(self, *_a, **_kw) -> list[dict]:
            return []

    with patch("server.core.ch.get_client", return_value=_EmptyCH()):
        out = await handle_profile_activity(inp)
    assert out.entries == []


@pytest.mark.asyncio
async def test_profile_render_serves_only_live_sections(monkeypatch) -> None:
    """memory.move leaves the moved-from section as status='duplicate' (not tombstoned),
    so the render must read the served statuses, and never an expired section."""
    from server.memory.api.models_profile import ProfileRenderInput
    from server.memory.app.profile import handle_profile_render

    seen: list[str] = []

    class _CH:
        async def query_all(self, sql, params=None, **kw):
            seen.append(sql)
            return []

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    await handle_profile_render(ProfileRenderInput(org="o", namespace="n", agent="a"))
    [sql] = [q for q in seen if "memory_type = 'profile'" in q]
    assert "(expires_at IS NULL OR expires_at > now())" in sql
    assert "status IN ('active', 'conflicted')" in sql


def test_profile_set_refuses_the_delete_version_marker() -> None:
    # The handler copies source_trace into memory.set; the input refuses the marker first,
    # so the caller gets a 422 rather than a 500 from inside the handler.
    import pydantic

    from server.memory.api.models_profile import ProfileSetInput

    with pytest.raises(pydantic.ValidationError, match="may not start with"):
        ProfileSetInput(
            org="o",
            namespace="n",
            agent="a",
            label="persona",
            value="v",
            source_trace="memory.delete_version:1",
        )
