"""memory.redact reaches the profiles a model built from the redacted memory.

A profile version names the memories it was built from in its trace
(`profile.fold:<label>:<memory ids>`); redacting one of them scrubs every
version that names it, and leaves the versions built without it. A lock on
the profile key refuses the redact before anything is scrubbed. However many
versions match, the named version and the profile versions are scrubbed in one
statement per version table, and the cached answers and precedents whose window
held a scrubbed profile key are deleted. A retry after a failure reaches the
same profile keys. A version that does not exist scrubs nothing. The memory and
each profile key it scrubbed are announced as `memory.redacted`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from clickhouse_connect.driver.binding import format_bind_value

from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

_SECRET = "00000000-0000-0000-0000-00000000000a"
_OTHER = "00000000-0000-0000-0000-00000000000b"
_PROFILE = "00000000-0000-0000-0000-0000000000ff"


def _set_params(params: dict[str, Any] | None) -> list[str]:
    """`SET param_<name>` statements, so a statement's parameters stay bound when
    the script replays it before a later query."""
    sets = []
    for name, value in (params or {}).items():
        text = format_bind_value(value).replace("\\", "\\\\").replace("'", "\\'")
        sets.append(f"SET param_{name} = '{text}'")
    return sets


class _StatefulLocalCH:
    """clickhouse-local that keeps every executed statement: each query replays them."""

    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)
        self.executed: list[str] = []
        self.bound: list[str] = []

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.setup += [*_set_params(params), sql]
        self.executed.append(sql)
        self.bound += [format_bind_value(v) for v in (params or {}).values()]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run([*self.setup, *_set_params(params)], sql)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None


def _profile_version(table: str, version: int, value: str, sources: list[str]) -> str:
    trace = "profile.fold:structured_profile:" + ",".join(sources)
    return (
        f"INSERT INTO loom.{table} (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, memory_id, content_sha256, version, source_trace) VALUES "
        f"('o', 'n', '-', 'u1', '-', 'structured_profile', 'profile', '{value}', "
        f"'{_PROFILE}', '{'a' * 64}', {version}, '{trace}')"
    )


_PROFILE_TABLES = (
    "SET enable_quantized_codec = 1",
    ddl("memory"),
    ddl("memory_versions"),
    ddl("memory_embeddings"),
    ddl("memory_chunks"),
    ddl("memory_code_links"),
)


def _profile_rows(built_with: str, built_without: str) -> list[str]:
    """Versions 1-3 were built from the secret memory, version 4 (the head) was not."""
    return [
        _profile_version(table, version, value, sources)
        for table in ("memory", "memory_versions")
        for version, value, sources in (
            (1, built_with, [_SECRET, _OTHER]),
            (2, built_with, [_OTHER, _SECRET]),
            (3, built_with, [_SECRET]),
            (4, built_without, [_OTHER]),
        )
    ]


_TARGET_KW: dict[str, Any] = {
    "org": "o",
    "memory_id": _SECRET,
    "namespace": "n",
    "agent": "a",
    "user_id": "u1",
    "session_id": "-",
    "memory_key": "user_timezone",
    "version": 1,
    "tombstoned_at": None,
}


class _Head:
    """The profile key's head, version 4."""

    org = "o"
    namespace = "n"
    agent = "-"
    user_id = "u1"
    session_id = "-"
    memory_key = "structured_profile"
    memory_id = _PROFILE
    version = 4
    lock_mode = ""
    lock_until = None


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_redact_scrubs_every_profile_version_built_from_the_memory(monkeypatch) -> None:
    import server.memory.store as memory_repo
    from server.memory.api.models_crud import MemoryRedactInput
    from server.memory.app import curation

    ch = _redact_setup(monkeypatch)

    out = await curation.handle_memory_redact(
        MemoryRedactInput(org="o", memory_id=_SECRET, version=1, reason="pii")
    )

    assert out.redacted
    # The named version and the three profile versions that name the memory go in one
    # statement per version table.
    assert sum(1 for sql in ch.executed if "UPDATE" in sql and "redacted_at" in sql) == 2
    for table in ("memory", "memory_versions"):
        values = await ch.query_all(
            f"SELECT version, value, redacted_at IS NOT NULL AS redacted FROM loom.{table} "
            "WHERE memory_key = 'structured_profile' ORDER BY version"
        )
        assert [(int(r["version"]), r["redacted"]) for r in values] == [
            (1, 1),
            (2, 1),
            (3, 1),
            (4, 0),
        ]
        assert not any("secret place" in r["value"] for r in values)
        assert values[3]["value"] == '{"timezone":null}'
    fact = await ch.query_all(
        "SELECT redacted_at IS NOT NULL AS redacted FROM loom.memory_versions "
        "WHERE memory_key = 'user_timezone'"
    )
    assert [r["redacted"] for r in fact] == [1]
    # A retried redaction reaches the same profile key.
    keys = await memory_repo.profile_keys_built_from("o", "n", "u1", _SECRET)
    assert [(r["memory_key"], int(r["newest"])) for r in keys] == [("structured_profile", 3)]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_redact_scrubs_more_profile_versions_than_one_parameter_holds(monkeypatch) -> None:
    from server.core.ch_params import FIELD_BYTES, chunks
    from server.memory.api.models_crud import MemoryRedactInput
    from server.memory.app import curation

    ch = _redact_setup(monkeypatch)
    # Versions 5..3004 were built from the secret memory too: listed as
    # (memory_id, version) pairs, they would not fit in one bound parameter.
    newest = 3004
    assert len(list(chunks([(_PROFILE, v) for v in range(5, newest + 1)]))) > 1
    trace = f"profile.fold:structured_profile:{_OTHER},{_SECRET}"
    for table in ("memory", "memory_versions"):
        ch.setup.append(
            f"INSERT INTO loom.{table} (org, namespace, agent, user_id, session_id, "
            "memory_key, memory_type, value, memory_id, content_sha256, version, "
            "source_trace) SELECT 'o', 'n', '-', 'u1', '-', 'structured_profile', "
            f"'profile', 'secret place', '{_PROFILE}', '{'a' * 64}', number + 5, "
            f"'{trace}' FROM numbers({newest - 4})"
        )

    class _NewestHead(_Head):
        version = newest

    async def head(*_a: Any, **_k: Any) -> _NewestHead:
        return _NewestHead()

    monkeypatch.setattr("server.memory.store.get_by_key", head)

    out = await curation.handle_memory_redact(
        MemoryRedactInput(org="o", memory_id=_SECRET, version=1, reason="pii")
    )

    assert out.redacted
    assert sum(1 for sql in ch.executed if "UPDATE" in sql and "redacted_at" in sql) == 2
    assert max(len(v) for v in ch.bound) < FIELD_BYTES
    rows = await ch.query_all(
        "SELECT version FROM loom.memory_versions "
        "WHERE memory_key = 'structured_profile' AND redacted_at IS NULL"
    )
    assert [int(r["version"]) for r in rows] == [4]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["read_only", "no_delete"])
async def test_a_locked_profile_refuses_the_redact_before_any_scrub(monkeypatch, mode) -> None:
    from fastapi import HTTPException

    import server.memory.store as memory_repo
    from server.memory.app import curation
    from server.memory.store.history import HistoryTarget

    async def versions(*_a: Any) -> list[dict[str, Any]]:
        return [{"agent": "-", "session_id": "-", "memory_key": "structured_profile", "newest": 1}]

    class _Locked(_Head):
        lock_mode = mode

    async def head(*_a: Any, **_k: Any) -> _Locked:
        return _Locked()

    async def no_scrub(*_a: Any, **_k: Any) -> None:
        raise AssertionError("nothing is scrubbed under a lock")

    monkeypatch.setattr(memory_repo, "profile_keys_built_from", versions)
    monkeypatch.setattr(memory_repo, "get_by_key", head)
    monkeypatch.setattr(memory_repo, "redact_versions", no_scrub)
    with pytest.raises(HTTPException) as e:
        await curation._profiles_built_from("o", HistoryTarget(**_TARGET_KW), _SECRET)
    assert e.value.status_code == 423


# The secret memory's version 1: the version memory.redact names.
_SECRET_VERSION = (
    "INSERT INTO loom.memory_versions (org, namespace, agent, user_id, session_id, memory_key, "
    "memory_type, value, memory_id, content_sha256, version) VALUES "
    f"('o', 'n', 'a', 'u1', '-', 'user_timezone', 'fact', '\"secret place\"', '{_SECRET}', "
    f"'{'b' * 64}', 1)"
)


def _redact_setup(monkeypatch: pytest.MonkeyPatch) -> _StatefulLocalCH:
    """The secret memory, the profile versions built from it, and one cached answer
    and one precedent whose window held the profile key; memory.redact's lookups
    other than the version reads are stubbed."""
    import server.auth.authz as authz
    import server.memory.store as memory_repo
    from server.memory.app import curation
    from server.memory.store import history, redaction
    from server.memory.store.history import HistoryTarget

    ch = _StatefulLocalCH(
        *_PROFILE_TABLES,
        ddl("llm_response_cache"),
        ddl("memory_ask_precedents"),
        _SECRET_VERSION,
        *_profile_rows('{"timezone":"secret place"}', '{"timezone":null}'),
        "INSERT INTO loom.llm_response_cache (model, key, org, text, window_keys) VALUES "
        "('m', 'k1', 'o', 'secret place', ['structured_profile']), "
        "('m', 'k2', 'o', 'other', ['user_name'])",
        "INSERT INTO loom.memory_ask_precedents (org, namespace, precedent_key, question, "
        f"answer, window_keys) VALUES ('o', 'n', '{'p' * 64}', 'where?', 'secret place', "
        "['structured_profile'])",
    )
    monkeypatch.setattr(redaction, "_default_client", lambda: ch)
    monkeypatch.setattr(history, "_default_client", lambda: ch)

    async def target(*_a: Any) -> HistoryTarget:
        return HistoryTarget(**_TARGET_KW)

    async def in_scope(*_a: Any) -> bool:
        return True

    async def head(*_a: Any, **_k: Any) -> _Head:
        return _Head()

    async def no_links(*_a: Any) -> str:
        return ""

    monkeypatch.setattr(memory_repo, "find_history_target", target)
    monkeypatch.setattr(authz, "row_in_scope", in_scope)
    monkeypatch.setattr(memory_repo, "get_by_key", head)
    monkeypatch.setattr(curation, "_live_link_inputs", no_links)
    return ch


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_redact_deletes_the_answers_whose_window_held_a_scrubbed_profile(
    monkeypatch,
) -> None:
    from server.memory.api.models_crud import MemoryRedactInput
    from server.memory.app import curation

    ch = _redact_setup(monkeypatch)

    out = await curation.handle_memory_redact(
        MemoryRedactInput(org="o", memory_id=_SECRET, version=1, reason="pii")
    )

    assert out.redacted and out.cache_rows_deleted == 1 and out.precedent_rows_deleted == 1
    cached = await ch.query_all("SELECT key FROM loom.llm_response_cache")
    assert [r["key"] for r in cached] == ["k2"]
    assert await ch.query_all("SELECT answer FROM loom.memory_ask_precedents") == []


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_retried_redact_scrubs_the_profile_rows_a_failed_one_left(monkeypatch) -> None:
    from server.memory.api.models_crud import MemoryRedactInput
    from server.memory.app import curation

    ch = _redact_setup(monkeypatch)
    # The profile key's embedding rows: one of a version built from the secret memory,
    # one of the live head.
    ch.setup.append(
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
        "memory_key, memory_type, scope, content_excerpt, embedding_model, version, "
        "embedding, memory_key_embedding) VALUES "
        "('o', 'n', '-', 'u1', '-', 'structured_profile', 'profile', 'private', "
        "'secret place', 'm', 1, arrayWithConstant(3072, toBFloat16(0)), "
        "arrayWithConstant(3072, toBFloat16(0))), "
        "('o', 'n', '-', 'u1', '-', 'structured_profile', 'profile', 'private', "
        "'{\"timezone\":null}', 'm', 2, arrayWithConstant(3072, toBFloat16(0)), "
        "arrayWithConstant(3072, toBFloat16(0)))"
    )
    scrub_profiles = curation._scrub_profile_derivatives

    async def fails(*_a: Any) -> None:
        raise TimeoutError("DELETE on memory_embeddings timed out")

    monkeypatch.setattr(curation, "_scrub_profile_derivatives", fails)
    redact = MemoryRedactInput(org="o", memory_id=_SECRET, version=1, reason="pii")
    with pytest.raises(TimeoutError):
        await curation.handle_memory_redact(redact)

    monkeypatch.setattr(curation, "_scrub_profile_derivatives", scrub_profiles)
    out = await curation.handle_memory_redact(redact)

    assert out.redacted and out.cache_rows_deleted == 1 and out.precedent_rows_deleted == 1
    excerpts = await ch.query_all(
        "SELECT content_excerpt FROM loom.memory_embeddings WHERE memory_key = 'structured_profile'"
    )
    assert [r["content_excerpt"] for r in excerpts] == ['{"timezone":null}']
    cached = await ch.query_all("SELECT key FROM loom.llm_response_cache")
    assert [r["key"] for r in cached] == ["k2"]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_redact_announces_the_memory_and_each_profile_key_it_scrubbed(monkeypatch) -> None:
    """A receiver that mirrors the profile key hears that its versions were scrubbed,
    as one that mirrors the memory's own key does."""
    from server.memory.api.models_crud import MemoryRedactInput
    from server.memory.app import curation, webhooks
    from server.memory.store import webhooks as webhook_store
    from tests.unit.ch_fake import RecordingCH

    _redact_setup(monkeypatch)
    outbox = RecordingCH()
    monkeypatch.setattr(webhook_store, "_default_client", lambda: outbox)
    hooks = {("o", "n"): (("wh1", frozenset({"memory.redacted"})),)}
    webhooks._registry._reset_for_tests(hooks, read_age_s=0.0, due_in_s=float("inf"))
    try:
        await curation.handle_memory_redact(
            MemoryRedactInput(org="o", memory_id=_SECRET, version=1, reason="pii")
        )
    finally:
        webhooks._registry._reset_for_tests(None, read_age_s=None, due_in_s=0.0)

    rows = outbox.inserted("memory_webhook_outbox")
    assert [(r["event_type"], r["memory_key"], r["memory_version"]) for r in rows] == [
        ("memory.redacted", "user_timezone", 1),
        ("memory.redacted", "structured_profile", 3),
    ]
    assert rows[1]["memory_id"] == _PROFILE


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_redacting_a_version_that_does_not_exist_scrubs_nothing(monkeypatch) -> None:
    from server.memory.api.models_crud import MemoryRedactInput
    from server.memory.app import curation

    ch = _redact_setup(monkeypatch)

    out = await curation.handle_memory_redact(
        MemoryRedactInput(org="o", memory_id=_SECRET, version=7, reason="pii")
    )

    assert not out.redacted
    assert ch.executed == []
    profile = await ch.query_all(
        "SELECT version FROM loom.memory_versions "
        "WHERE memory_key = 'structured_profile' AND redacted_at IS NULL ORDER BY version"
    )
    assert [int(r["version"]) for r in profile] == [1, 2, 3, 4]
    cached = await ch.query_all("SELECT key FROM loom.llm_response_cache ORDER BY key")
    assert [r["key"] for r in cached] == ["k1", "k2"]
    assert len(await ch.query_all("SELECT answer FROM loom.memory_ask_precedents")) == 1


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_redacted_profile_head_leaves_render_and_profile_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.ch as core_ch
    import server.memory.store as memory_repo
    from server.memory.api.models_profile import ProfileRenderInput
    from server.memory.app import curation
    from server.memory.app.profile import handle_profile_render
    from server.memory.app.profile_search import PROFILE_FETCH_SQL
    from server.memory.store import redaction
    from server.memory.store.history import HistoryTarget

    # The live head (version 2) was built from the secret memory.
    ch = _StatefulLocalCH(
        *_PROFILE_TABLES,
        *[
            _profile_version(table, version, '{"timezone":"secret place"}', sources)
            for table in ("memory", "memory_versions")
            for version, sources in ((1, [_OTHER]), (2, [_SECRET]))
        ],
    )
    monkeypatch.setattr(redaction, "_default_client", lambda: ch)
    monkeypatch.setattr(core_ch, "get_client", lambda: ch)

    class _LiveHead(_Head):
        version = 2

    async def head(*_a: Any, **_k: Any) -> _LiveHead:
        return _LiveHead()

    async def no_links(*_a: Any) -> str:
        return ""

    monkeypatch.setattr(memory_repo, "get_by_key", head)
    monkeypatch.setattr(curation, "_live_link_inputs", no_links)

    render_input = ProfileRenderInput(
        org="o", namespace="n", agent="a", user_id="u1", read_scope="own_user"
    )
    search_params = {"org": "o", "keys": [("n", "-", "u1", "-", "structured_profile")]}
    # Before the redact, both reads serve the section.
    assert (await handle_profile_render(render_input)).labels == ["structured_profile"]
    assert len(await ch.query_all(PROFILE_FETCH_SQL, search_params)) == 1

    target = HistoryTarget(**_TARGET_KW)
    profiles = await curation._profiles_built_from("o", target, _SECRET)
    await memory_repo.redact_versions(
        "o",
        "n",
        "u1",
        _SECRET,
        1,
        when=datetime(2026, 1, 1),
        reason_blob="r|admin",
    )
    await curation._scrub_profile_derivatives("o", target, profiles)

    rendered = await handle_profile_render(render_input)
    assert rendered.labels == [] and rendered.section_count == 0
    assert "REDACTED" not in rendered.rendered
    assert await ch.query_all(PROFILE_FETCH_SQL, search_params) == []
