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
    "content_sha256": "b" * 64,
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
    redacted_at = None
    content_sha256 = "d" * 64


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
    """The secret memory, the profile versions built from it, one cached answer, one
    cached code.ask answer and one precedent whose window held the profile key;
    memory.redact's lookups other than the version reads are stubbed."""
    import server.auth.authz as authz
    import server.memory.store as memory_repo
    from server.memory.app import curation
    from server.memory.store import history, redaction

    ch = _StatefulLocalCH(
        *_PROFILE_TABLES,
        ddl("llm_response_cache"),
        ddl("memory_ask_precedents"),
        ddl("code_answers"),
        _SECRET_VERSION,
        *_profile_rows('{"timezone":"secret place"}', '{"timezone":null}'),
        "INSERT INTO loom.llm_response_cache (model, key, org, text, window_keys) VALUES "
        "('m', 'k1', 'o', 'secret place', ['structured_profile']), "
        "('m', 'k2', 'o', 'other', ['user_name'])",
        "INSERT INTO loom.code_answers (org, namespace, question_id, question_embedding, "
        "answer, memory_namespace, memory_keys) VALUES "
        "('o', 'code', generateUUIDv4(), arrayWithConstant(3072, toBFloat16(0)), "
        "'secret place', 'n', ['structured_profile']), "
        "('o', 'code', generateUUIDv4(), arrayWithConstant(3072, toBFloat16(0)), "
        "'other', 'n', ['user_name'])",
        "INSERT INTO loom.memory_ask_precedents (org, namespace, precedent_key, question, "
        f"answer, window_keys) VALUES ('o', 'n', '{'p' * 64}', 'where?', 'secret place', "
        "['structured_profile'])",
    )
    monkeypatch.setattr(redaction, "_default_client", lambda: ch)
    monkeypatch.setattr(history, "_default_client", lambda: ch)

    async def in_scope(*_a: Any) -> bool:
        return True

    async def head(*_a: Any, **_k: Any) -> _Head:
        return _Head()

    async def no_links(*_a: Any) -> str:
        return ""

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

    assert out.redacted and out.cache_rows_deleted == 2 and out.precedent_rows_deleted == 1
    cached = await ch.query_all("SELECT key FROM loom.llm_response_cache")
    assert [r["key"] for r in cached] == ["k2"]
    code = await ch.query_all("SELECT answer FROM loom.code_answers")
    assert [r["answer"] for r in code] == ["other"]
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

    async def fails(*_a: Any, **_k: Any) -> None:
        raise TimeoutError("DELETE on memory_embeddings timed out")

    monkeypatch.setattr(curation, "_scrub_profile_derivatives", fails)
    redact = MemoryRedactInput(org="o", memory_id=_SECRET, version=1, reason="pii")
    with pytest.raises(TimeoutError):
        await curation.handle_memory_redact(redact)

    monkeypatch.setattr(curation, "_scrub_profile_derivatives", scrub_profiles)
    out = await curation.handle_memory_redact(redact)

    assert out.redacted and out.cache_rows_deleted == 2 and out.precedent_rows_deleted == 1
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
        ("o", "n", "a", "u1", "-", "user_timezone"),
        _SECRET,
        1,
        when=datetime(2026, 1, 1),
        reason_blob="r|admin",
        head=None,
    )
    await curation._scrub_profile_derivatives("o", target, profiles, through=2**63)

    rendered = await handle_profile_render(render_input)
    assert rendered.labels == [] and rendered.section_count == 0
    assert "REDACTED" not in rendered.rendered
    assert await ch.query_all(PROFILE_FETCH_SQL, search_params) == []


def _version(
    table: str, version: int, value: str, *, deleted: bool = False, ns: str = "n", agent: str = "a"
) -> str:
    return (
        f"INSERT INTO loom.{table} (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, memory_id, content_sha256, version, tombstoned_at) VALUES "
        f"('o', '{ns}', '{agent}', 'u1', '-', 'k', 'fact', '\"{value}\"', '{_SECRET}', "
        f"'{value.lower()[0] * 64}', {version}, {'now()' if deleted else 'NULL'})"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history", "copies"),
    [
        # The delete right after the version copied its text: scrubbed with it.
        ([("A", False), ("A", True)], [2]),
        # A copy writer (an entity pass, a lock) before the delete carries it too.
        ([("A", False), ("A", False), ("A", True)], [2, 3]),
        # The same text written again and then deleted is not the redacted version.
        ([("A", False), ("B", False), ("A", False), ("A", True)], []),
    ],
)
async def test_the_later_copies_of_a_redacted_version_are_scrubbed(
    history: list[tuple[str, bool]], copies: list[int]
) -> None:
    """The later versions of the key that carry the redacted one's text are scrubbed with
    it; another key holding the same id is left alone."""
    from server.memory.store import redaction

    ch = _StatefulLocalCH(
        ddl("memory"),
        ddl("memory_versions"),
        *(
            _version(table, v, value, deleted=deleted)
            for table in ("memory", "memory_versions")
            for v, (value, deleted) in enumerate(history, start=1)
        ),
        # A namespace restored from this one, and this one restored into itself under
        # another agent: each keeps the id, and neither is the redacted key.
        _version("memory_versions", 8, "A", deleted=True, ns="restored"),
        _version("memory_versions", 9, "A", deleted=True, agent="b"),
    )
    scrubbed = await redaction.redact_versions(
        ("o", "n", "a", "u1", "-", "k"),
        _SECRET,
        1,
        when=datetime(2026, 1, 2),
        reason_blob="pii|t",
        head=None,
        ch=ch,
    )
    assert scrubbed == copies
    for table in ("memory", "memory_versions"):
        rows = await ch.query_all(
            f"SELECT namespace, agent, version FROM loom.{table} "
            "WHERE value = '\"[REDACTED]\"' ORDER BY version"
        )
        assert [(r["namespace"], r["agent"], int(r["version"])) for r in rows] == [
            ("n", "a", v) for v in [1, *copies]
        ], table


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_the_redacted_version_names_its_own_key() -> None:
    """A namespace restored into itself under another agent holds the memory's id under a
    second key, at newer versions: the redaction of a version acts on the key that wrote
    it."""
    from server.memory.store import history

    ch = _StatefulLocalCH(
        ddl("memory"),
        ddl("memory_versions"),
        _version("memory_versions", 1, "A"),
        _version("memory_versions", 5, "A", agent="b"),
    )
    target = await history.find_history_target("o", _SECRET, 1, ch=ch)
    assert target is not None and (target.agent, target.version) == ("a", 1)


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("head", "scrubbed"),
    [
        # The key's live head is this memory's v2; a touch wrote v3 since it was read.
        ((_SECRET, 2), [1]),
        # The key's live head is another memory's: every copy of this one goes.
        (("00000000-0000-0000-0000-0000000000cc", 9), [1, 2, 3]),
        # Deleted at the read (v2), then restored during the redaction (v3): the text the
        # restore brought back is scrubbed too.
        (None, [1, 2, 3]),
    ],
)
async def test_a_redaction_leaves_the_live_head_and_what_was_copied_from_it(
    head: tuple[str, int] | None, scrubbed: list[int]
) -> None:
    """On the ClickHouse authority no lock holds the head: nothing from the head the
    caller read on is scrubbed, a copy written from it since included."""
    from server.memory.store import redaction

    ch = _StatefulLocalCH(
        ddl("memory"),
        ddl("memory_versions"),
        *(_version("memory_versions", v, "A") for v in (1, 2, 3)),
    )
    await redaction.redact_versions(
        ("o", "n", "a", "u1", "-", "k"),
        _SECRET,
        1,
        when=datetime(2026, 1, 2),
        reason_blob="pii|t",
        head=head,
        ch=ch,
    )
    rows = await ch.query_all(
        "SELECT version FROM loom.memory_versions WHERE value = '\"[REDACTED]\"' ORDER BY version"
    )
    assert [int(r["version"]) for r in rows] == scrubbed


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize(("head_live", "kept"), [(True, [22]), (False, [22])])
async def test_a_newer_write_stays_unless_it_carries_the_redacted_text(
    head_live: bool, kept: list[int]
) -> None:
    """An embedding row stamped after the redaction began is a newer write's and stays,
    with a live head or none, unless it carries the redacted text (a restore's mirror of
    the old excerpt). A chunk of the secret goes whatever second it was written."""
    from server.memory.store import redaction

    def _embedding(version: int, text: str, sha: str = "") -> str:
        return (
            "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
            "memory_key, memory_type, scope, content_excerpt, embedding_model, version, "
            "content_sha256, embedding, memory_key_embedding) VALUES "
            f"('o', 'n', 'a', 'u1', '-', 'k', 'fact', 'private', '{text}', 'm', {version}, "
            f"'{sha}', arrayWithConstant(3072, toBFloat16(0)), "
            "arrayWithConstant(3072, toBFloat16(0)))"
        )

    ch = _StatefulLocalCH(
        *_PROFILE_TABLES,
        _embedding(5, "the secret"),
        _embedding(21, "the secret", sha="f" * 64),
        _embedding(22, "a newer write", sha="c" * 64),  # stamped after the redaction began
        # The deleted version's chunk: not the live head's, whatever second it was written.
        "INSERT INTO loom.memory_chunks (org, namespace, agent, user_id, session_id, "
        "chunk_index, content_sha256, content, embedding, embedding_model, parent_memory_key, "
        f"tombstoned_at) VALUES ('o', 'n', 'a', 'u1', '-', 0, '{'e' * 64}', 'the secret', "
        "arrayWithConstant(3072, toBFloat16(0)), 'm', 'k', now())",
    )
    scrubs = await redaction.key_derivative_scrubs(
        "o",
        "n",
        "a",
        "u1",
        "-",
        "k",
        head_live=head_live,
        through=10,
        redacted_sha="f" * 64,
        ch=ch,
        set_chunks_kept=None,
    )
    await redaction.run_scrubs(scrubs, apply=True, ch=ch)
    rows = await ch.query_all("SELECT version FROM loom.memory_embeddings ORDER BY version")
    assert [int(r["version"]) for r in rows] == kept
    assert await ch.query_all("SELECT content FROM loom.memory_chunks") == []


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize(("live_sha", "kept"), [("a" * 64, []), ("f" * 64, [1])])
async def test_a_row_of_the_redacted_text_goes_though_clickhouse_still_reads_it_live(
    live_sha: str, kept: list[int]
) -> None:
    """Under Postgres the live head's own search row may not be in ClickHouse yet, so the
    redacted version's row still reads as the key's live one: it goes by its hash, unless
    the live head carries that very text."""
    from server.memory.store import redaction

    ch = _StatefulLocalCH(
        *_PROFILE_TABLES,
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
        "memory_key, memory_type, scope, content_excerpt, embedding_model, version, "
        "content_sha256, embedding, memory_key_embedding) VALUES "
        f"('o', 'n', 'a', 'u1', '-', 'k', 'fact', 'private', 'the secret', 'm', 1, "
        f"'{'f' * 64}', arrayWithConstant(3072, toBFloat16(0)), "
        "arrayWithConstant(3072, toBFloat16(0)))",
    )
    scrubs = await redaction.key_derivative_scrubs(
        "o",
        "n",
        "a",
        "u1",
        "-",
        "k",
        head_live=True,
        through=10,
        redacted_sha="f" * 64,
        live_sha=live_sha,
        ch=ch,
        set_chunks_kept=None,
    )
    await redaction.run_scrubs(scrubs, apply=True, ch=ch)
    rows = await ch.query_all("SELECT version FROM loom.memory_embeddings")
    assert [int(r["version"]) for r in rows] == kept


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_row_of_other_text_goes_though_it_still_reads_live_in_clickhouse() -> None:
    """Under Postgres a profile key's newer fold may not be in ClickHouse yet, so the old
    fold built from the redacted memory still reads live there: it goes, as its hash is
    not the live head's. A row of the live head's hash, and one a newer write stamped
    since, stay."""
    from server.memory.store import redaction

    def _row(version: int, text: str, sha: str) -> str:
        return (
            "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
            "memory_key, memory_type, scope, content_excerpt, embedding_model, version, "
            "content_sha256, embedding, memory_key_embedding) VALUES "
            f"('o', 'n', '-', 'u1', '-', 'profile', 'profile', 'private', '{text}', 'm', "
            f"{version}, '{sha}', arrayWithConstant(3072, toBFloat16(0)), "
            "arrayWithConstant(3072, toBFloat16(0)))"
        )

    ch = _StatefulLocalCH(
        *_PROFILE_TABLES,
        _row(3, "the live fold", "a" * 64),
        _row(4, "a fold of the secret", "b" * 64),
        _row(20, "a newer fold", "c" * 64),
    )
    scrubs = await redaction.key_derivative_scrubs(
        "o",
        "n",
        "-",
        "u1",
        "-",
        "profile",
        head_live=True,
        through=10,
        live_sha="a" * 64,
        ch=ch,
        set_chunks_kept=None,
    )
    await redaction.run_scrubs(scrubs, apply=True, ch=ch)
    rows = await ch.query_all("SELECT version FROM loom.memory_embeddings ORDER BY version")
    assert [int(r["version"]) for r in rows] == [3, 20]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("through", [10, None])
async def test_an_unhashed_late_row_is_judged_by_its_excerpt(through: int | None) -> None:
    """A row with no content hash cannot be matched to a write: stamped after the
    redaction began, it is judged by its excerpt, not taken for a newer write. With no
    live head it goes, and a dropped profile head takes no bound at all."""
    from server.memory.store import redaction

    def _row(version: int, text: str) -> str:
        return (
            "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
            "memory_key, memory_type, scope, content_excerpt, embedding_model, version, "
            "content_sha256, embedding, memory_key_embedding) VALUES "
            f"('o', 'n', '-', 'u1', '-', 'profile', 'profile', 'private', '{text}', 'm', "
            f"{version}, '', arrayWithConstant(3072, toBFloat16(0)), "
            "arrayWithConstant(3072, toBFloat16(0)))"
        )

    ch = _StatefulLocalCH(*_PROFILE_TABLES, _row(5, "an old fold"), _row(20, "the secret fold"))
    scrubs = await redaction.key_derivative_scrubs(
        "o",
        "n",
        "-",
        "u1",
        "-",
        "profile",
        head_live=False,
        through=through,
        ch=ch,
        set_chunks_kept=None,
    )
    await redaction.run_scrubs(scrubs, apply=True, ch=ch)
    assert await ch.query_all("SELECT version FROM loom.memory_embeddings") == []


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("postgres", "heads_write", "kept"),
    [
        (True, ["d", "e"], ["d", "e", "x"]),
        (True, ["d"], ["d", "x"]),
        # ClickHouse is the authority: what it holds as live stays, as the heads' own.
        (False, ["d"], ["d", "e", "x"]),
    ],
)
async def test_a_set_chunk_no_live_head_writes_goes_though_clickhouse_holds_it_live(
    postgres: bool, heads_write: list[str], kept: list[str]
) -> None:
    """Under Postgres the live head's chunks may not have replaced the redacted value's in
    ClickHouse yet: a `memory.set` chunk goes unless a live head writes it. A conversation
    chunk of the key is judged by what ClickHouse holds as live, and stays."""
    from server.memory.store import redaction

    def _chunk(session: str, index: int, sha: str) -> str:
        return (
            "INSERT INTO loom.memory_chunks (org, namespace, agent, user_id, session_id, "
            "chunk_index, content_sha256, content, embedding, embedding_model, "
            f"parent_memory_key) VALUES ('o', 'n', 'a', 'u1', '{session}', {index}, "
            f"'{sha * 64}', '{sha}', arrayWithConstant(3072, toBFloat16(0)), 'm', 'k')"
        )

    ch = _StatefulLocalCH(
        *_PROFILE_TABLES,
        _chunk("memory.set:k", 0, "d"),
        _chunk("memory.set:k", 1, "e"),
        _chunk("conversation", 0, "x"),
    )
    scrubs = await redaction.key_derivative_scrubs(
        "o",
        "n",
        "a",
        "u1",
        "-",
        "k",
        head_live=True,
        through=10,
        set_chunks_kept=[sha * 64 for sha in heads_write] if postgres else None,
        ch=ch,
    )
    await redaction.run_scrubs(scrubs, apply=True, ch=ch)
    rows = await ch.query_all("SELECT content FROM loom.memory_chunks ORDER BY content")
    assert [r["content"] for r in rows] == kept


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize(("head_live", "through"), [(False, None), (True, 5)])
async def test_a_fence_row_stays(head_live: bool, through: int | None) -> None:
    """A fence (no excerpt, terms or vector, as `fence_vectors` writes it) holds no text:
    it stays, keeping an older vector still in flight hidden under the 'high' head it was
    written for, with a live head or none, above the redaction's bound or not. A drop
    tombstone that kept the redacted value's terms goes."""
    from server.memory.store import redaction

    ch = _StatefulLocalCH(
        *_PROFILE_TABLES,
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
        "memory_key, memory_type, scope, content_excerpt, embedding_model, version, "
        "embedding_dim, embedding, memory_key_embedding, tombstoned_at) VALUES "
        # The placeholder vector the insert puts in an empty one's place.
        "('o', 'n', 'a', 'u1', '-', 'k', 'fact', 'private', '', 'm', 7, 0, "
        "arrayWithConstant(3072, toBFloat16(0)), arrayWithConstant(3072, toBFloat16(0)), now())",
        # A worker's drop tombstone of a stub: no excerpt or vector, the stub's terms kept.
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
        "memory_key, memory_type, scope, content_excerpt, embedding_model, version, "
        "embedding_dim, entities, content_sha256, embedding, memory_key_embedding, "
        "tombstoned_at) VALUES ('o', 'n', 'a', 'u1', '-', 'k', 'fact', 'private', '', 'm', 3, "
        f"0, ['the secret'], '{'a' * 64}', arrayWithConstant(3072, toBFloat16(0)), "
        "arrayWithConstant(3072, toBFloat16(0)), now())",
    )
    scrubs = await redaction.key_derivative_scrubs(
        "o",
        "n",
        "a",
        "u1",
        "-",
        "k",
        head_live=head_live,
        through=through,
        redacted_sha="a" * 64,
        set_chunks_kept=None,
        ch=ch,
    )
    await redaction.run_scrubs(scrubs, apply=True, ch=ch)
    rows = await ch.query_all("SELECT version FROM loom.memory_embeddings")
    assert [int(r["version"]) for r in rows] == [7]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_the_set_chunk_count_reaches_retired_rows_and_no_other_session() -> None:
    from server.memory.store import redaction

    def _chunk(session: str, index: int, retired: bool = False) -> str:
        return (
            "INSERT INTO loom.memory_chunks (org, namespace, agent, user_id, session_id, "
            "chunk_index, content_sha256, content, embedding, embedding_model, "
            f"parent_memory_key, tombstoned_at) VALUES ('o', 'n', 'a', 'u1', '{session}', "
            f"{index}, '{'d' * 64}', 'x', arrayWithConstant(3072, toBFloat16(0)), 'm', 'k', "
            f"{'now()' if retired else 'NULL'})"
        )

    ch = _StatefulLocalCH(
        *_PROFILE_TABLES,
        _chunk("memory.set:k", 3),
        _chunk("memory.set:k", 40, retired=True),
        _chunk("conversation", 90),
    )
    assert await redaction.set_chunk_count("o", "n", "a", "u1", "k", ch=ch) == 41
    assert await redaction.set_chunk_count("o", "n", "a", "u1", "other", ch=ch) == 0


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_redact_announces_each_later_copy_it_scrubs(monkeypatch) -> None:
    """A restatement and a delete's tombstone copy the redacted text: each is scrubbed
    and announced, up to the version that holds other text."""
    from server.memory.api.models_crud import MemoryRedactInput
    from server.memory.app import curation, webhooks

    ch = _redact_setup(monkeypatch)
    for version, sha in ((2, "b" * 64), (3, "b" * 64), (5, "e" * 64)):
        ch.setup.append(_SECRET_VERSION.replace(f"'{'b' * 64}', 1)", f"'{sha}', {version})"))
    heard: list[tuple[str, str, int]] = []

    async def announce(events: list[tuple[str, Any]]) -> None:
        heard.extend(
            (kind, change["memory_key"], int(change["version"])) for kind, change in events
        )

    monkeypatch.setattr(webhooks, "announce", announce)
    out = await curation.handle_memory_redact(
        MemoryRedactInput(org="o", memory_id=_SECRET, version=1, reason="pii")
    )
    assert out.redacted
    # Versions 1-3 of the key hold the text; 5 holds other text. The profile key's
    # newest version built from the memory is 3.
    assert sorted(heard) == [
        ("memory.redacted", "structured_profile", 3),
        ("memory.redacted", "user_timezone", 1),
        ("memory.redacted", "user_timezone", 2),
        ("memory.redacted", "user_timezone", 3),
    ]
