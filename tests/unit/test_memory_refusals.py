"""Deleted content cannot return through another key or a containing passage."""

import pytest

from server.memory.domain.refusals import fingerprint, matches, tokens


@pytest.mark.parametrize("native", [False, True])
def test_erasure_captures_clickhouse_history_under_both_authorities(monkeypatch, native):
    from server.core import pg
    from server.erasure.store import refusals

    monkeypatch.setattr(pg, "enabled", lambda: native)
    assert "loom.memory_versions" in dict(refusals._sources())


@pytest.mark.parametrize("user_id", [None, "alice"])
async def test_native_erasure_keeps_cleared_revision_identity_without_fingerprinting_null(
    monkeypatch, user_id
):
    from types import SimpleNamespace

    from server.erasure.store import consent, refusals, steps_pg

    identity = {"org": "o", "namespace": "n", "agent": "a", "user_id": "alice", "memory_key": "k"}
    payload = {**identity, "value": '"current fact"', "value_json": "{}"}
    recorded, dependents = [], []

    class Connection:
        async def cursor(self, sql, *args):
            assert "coalesce(r.memory_key, r.row->>'memory_key'" in sql
            yield {**identity, "row": None}
            yield {**identity, "row": payload}

    async def record(conn, rows, **kwargs):
        assert all(isinstance(row, dict) for row in rows)
        recorded.extend(rows)

    async def capture(conn, rows, uid):
        assert uid == "alice"
        dependents.extend(rows)

    monkeypatch.setattr(consent, "current_policy", lambda: SimpleNamespace(record_postgres=record))
    monkeypatch.setattr(refusals, "capture_native_chunks", capture)
    await steps_pg._capture_history(Connection(), "r.org=$1", "org=$1", ["o"], user_id)
    assert recorded == [payload]
    assert dependents == ([identity, identity] if user_id else [])


@pytest.mark.parametrize("value", [None, "", 0, False, {"b": 2, "a": 1}, [1, 2]])
def test_canonical_values_are_refused(value):
    assert matches(value, fingerprint(value))
    assert matches({"wrapped": value}, fingerprint(value))
    assert not matches("unrelated", fingerprint(value))


def test_normalized_literal_is_refused_inside_later_text():
    policy = fingerprint("My code is ＡＢＣ-123")
    assert matches("Reminder: MY code is abc-123. Do not repeat it.", policy)
    assert not matches("My code is abc-124", policy)


def test_oversized_scan_refuses_instead_of_allowing_a_prefix():
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError):
        matches("other " * 250_001, fingerprint("forgotten value"))


@pytest.mark.parametrize("value", ["alpha", None, False, 17, 0, [], {}, {"k": "v"}])
def test_persisted_scalar_and_object_values_use_the_served_column(value):
    import json

    from server.core.hashing import served_row_value

    stored = {
        "value": json.dumps(value),
        "value_json": json.dumps(value) if isinstance(value, dict) else "{}",
    }
    assert served_row_value(stored) == value
    assert matches(served_row_value(stored), fingerprint(value))


def test_absent_raw_metadata_does_not_reintroduce_a_deleted_null():
    from server.core.hashing import served_row_value
    from server.memory.store.refusals import content_policy_row

    row = content_policy_row({"org": "o", "namespace": "n"}, ["an unrelated passage"])
    assert not matches(served_row_value(row), fingerprint(None))


@pytest.mark.parametrize("metadata", [None, "", "{}", " { } ", {}, [], "null"])
def test_default_raw_metadata_does_not_freeze_unrelated_intake(metadata):
    from server.memory.store.refusals import _raw_content_values

    values = _raw_content_values({"content": "an unrelated passage", "metadata": metadata})
    assert values == ["an unrelated passage"]


@pytest.mark.parametrize("content", ["null", "{}", "[]"])
def test_erasure_keeps_literal_content_distinct_from_empty_metadata(monkeypatch, content):
    import json

    from server.memory.store import refusals as policy

    monkeypatch.setattr(policy, "_writer", lambda: "process:test")
    row = {"org": "o", "namespace": "n", "value_json": "{}", "value": json.dumps(content)}
    captured = policy._policy([row])
    patterns = [(r["kind"], r["units"], r["digest"]) for r in captured]
    assert matches(policy._raw_content_values({"content": content}), patterns)


@pytest.mark.parametrize("metadata", [" { } ", " [ ] ", "null", '""', ""])
def test_erasure_ignores_only_decoded_empty_metadata(monkeypatch, metadata):
    from server.memory.store import refusals as policy

    monkeypatch.setattr(policy, "_writer", lambda: "process:test")
    row = {
        "org": "o",
        "namespace": "n",
        "value_json": "{}",
        "value": metadata,
        "capture_kind": "metadata",
    }
    assert policy._policy([row]) == []


def test_erased_structured_metadata_matches_later_metadata(monkeypatch):
    import json

    from server.memory.store import refusals as policy

    monkeypatch.setattr(policy, "_writer", lambda: "process:test")
    metadata = json.dumps({"secret": "copper lark"})
    row = {
        "org": "o",
        "namespace": "n",
        "value_json": "{}",
        "value": metadata,
        "capture_kind": "metadata",
    }
    captured = policy._policy([row])
    patterns = [(r["kind"], r["units"], r["digest"]) for r in captured]
    assert matches(
        policy._raw_content_values({"content": "unrelated", "metadata": metadata}), patterns
    )


async def test_candidate_lookups_fit_the_http_parameter_limit():
    from clickhouse_connect.driver.binding import format_bind_value

    from server.core.ch_params import FIELD_BYTES
    from server.memory.store import refusals as policy

    batches = []

    class CH:
        async def query_all(self, sql, params=None):
            if "SELECT DISTINCT" in sql:
                return [{"units": 1}]
            batch = params["candidates"]
            assert len(format_bind_value(batch).encode()) <= FIELD_BYTES
            batches.append(batch)
            return []

    value = " ".join(f"word{i}" for i in range(2100))
    await policy.check_clickhouse(
        CH(), [policy.content_policy_row({"org": "o", "namespace": "n"}, value)]
    )
    assert len(batches) >= 3
    assert sum(len(batch) for batch in batches) >= 2100


async def test_a_current_source_hash_cannot_publish_a_refused_excerpt(monkeypatch):
    from server.core.errors import ValidationError
    from server.core.hashing import sha256_value
    from server.memory.store import refusals as policy

    source = policy.content_policy_row({"org": "o", "namespace": "n"}, "current fact")
    excerpt = "retired secret"
    row = {**source, "content_sha256": sha256_value("current fact"), "content_excerpt": excerpt}
    patterns = set(fingerprint(excerpt))

    class CH:
        async def query_all(self, sql, params=None):
            if "SELECT DISTINCT" in sql:
                return [{"units": 2}]
            return [{"found": 1}] if patterns.intersection(params["candidates"]) else []

    async def heads(*args, **kwargs):
        return [row], [source]

    monkeypatch.setattr(policy, "_embedding_sources", heads)
    with pytest.raises(ValidationError, match="refused"):
        async with policy.publication(CH(), [row], embeddings=True):
            pytest.fail("The refused excerpt reached its publisher")


@pytest.mark.parametrize("native_bytes", [False, True])
async def test_embedding_admission_accepts_the_current_fixed_string_hash(monkeypatch, native_bytes):
    from server.core.hashing import sha256_value
    from server.memory.store import refusals as policy
    from server.memory.store.rows import identity_key
    from tests.unit.test_memory_verify import _row

    source = _row(value="current fact")
    digest = sha256_value(source.value)
    vector = {
        **source.model_dump(),
        "content_sha256": digest.encode() if native_bytes else digest,
    }

    async def heads(*args, **kwargs):
        return {identity_key(source): source}

    monkeypatch.setattr("server.memory.store.reads.memory_rows_by_identity_keys", heads)
    admitted, sources = await policy._embedding_sources(object(), [vector])
    assert admitted == [vector] and len(sources) == 1


@pytest.mark.parametrize(
    "change", [{"value": "a replacement"}, {"status": "rejected"}, {"tombstoned_at": "2026-01-01"}]
)
async def test_embedding_admission_withholds_a_changed_or_deleted_source(monkeypatch, change):
    from server.core.hashing import sha256_value
    from server.memory.store import refusals as policy
    from server.memory.store.rows import identity_key
    from tests.unit.test_memory_verify import _row

    source = _row(value="current fact")
    vector = {**source.model_dump(), "content_sha256": sha256_value(source.value)}
    current = source.model_copy(update=change)

    async def heads(*args, **kwargs):
        return {identity_key(current): current}

    monkeypatch.setattr("server.memory.store.reads.memory_rows_by_identity_keys", heads)
    assert await policy._embedding_sources(object(), [vector]) == ([], [])


@pytest.mark.parametrize("entry", ["handler", "queue"])
async def test_prior_transcript_messages_cannot_bypass_refusal(monkeypatch, entry):
    from server.core.errors import ValidationError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.api.models_bulk import MemorySetFromMessagesInput
    from server.memory.app import extraction_queue
    from server.memory.app.extract import handler

    secret = "The retired phrase is copper lark"
    patterns = set(fingerprint(secret))

    class CH:
        async def query_all(self, sql, params=None):
            if "SELECT DISTINCT" in sql:
                return [{"units": len(tokens(secret))}]
            return [{"found": 1}] if patterns.intersection(params["candidates"]) else []

        async def insert(self, *args, **kwargs):
            pytest.fail("Refused context reached persistent storage")

    ch = CH()
    monkeypatch.setattr(handler, "get_client", lambda: ch)
    request = MemorySetFromMessagesInput(
        org="o",
        namespace="n",
        agent="a",
        defer=True,
        messages=[{"role": "user", "content": "hello"}],
        last_messages=[{"role": "user", "content": secret}],
    )
    with pytest.raises(ValidationError, match="refused"):
        if entry == "handler":
            await handler.handle_memory_set_from_messages(request)
        else:
            await extraction_queue.enqueue(request, scope=WHOLE_NAMESPACE, ch=ch)


def test_every_extraction_field_has_an_explicit_content_classification():
    from server.memory.api.models_bulk import MemorySetFromMessagesInput
    from server.memory.domain.refusals import EXTRACTION_CONTENT_FIELDS, EXTRACTION_PROTOCOL_FIELDS

    assert not EXTRACTION_CONTENT_FIELDS & EXTRACTION_PROTOCOL_FIELDS
    assert EXTRACTION_CONTENT_FIELDS | EXTRACTION_PROTOCOL_FIELDS == set(
        MemorySetFromMessagesInput.model_fields
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("extraction_instructions", "retired copper lark"),
        ("session_values", ["retired copper lark"]),
        ("extra_tags", ["retired copper lark"]),
        ("recalled_keys", ["retired copper lark"]),
        ("record_schema", {"choice": ["retired copper lark"]}),
        ("pipeline_steps", [{"step": "custom", "note": "retired copper lark"}]),
        ("observation_date", "retired copper lark"),
        ("messages", [{"role": "user", "name": "retired copper lark", "content": "hello"}]),
        ("messages", [{"role": "retired copper lark", "content": "hello"}]),
    ],
)
@pytest.mark.parametrize("entry", ["queue", "transition", "capture"])
async def test_all_queued_text_obeys_the_same_refusal_policy(monkeypatch, field, value, entry):
    import json

    from server.core.errors import ValidationError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.api.models_bulk import MemorySetFromMessagesInput
    from server.memory.app import extraction_queue as queue
    from server.memory.store import refusals as policy
    from tests.unit.test_extraction_queue import _stored_row

    patterns = set(fingerprint("retired copper lark"))

    class CH:
        def __init__(self):
            self.ended = []

        async def query_all(self, sql, params=None):
            if "SELECT DISTINCT" in sql:
                return [{"units": 3}]
            return [{"found": 1}] if patterns.intersection(params["candidates"]) else []

        async def insert(self, *args, **kwargs):
            pytest.fail("Refused queue text was copied")

        async def execute(self, sql, params=None):
            assert "UPDATE status='failed'" in sql
            assert "retired copper lark" not in sql + repr(params)
            self.ended.append(params)

    request = MemorySetFromMessagesInput(
        **{
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "messages": [{"role": "user", "content": "hello"}],
            field: value,
        }
    )
    ch = CH()
    if entry == "capture":
        monkeypatch.setattr(policy, "_writer", lambda: "process:test")
        rows = policy._policy(
            [
                {
                    "org": "o",
                    "namespace": "n",
                    "value_json": "{}",
                    "value": request.model_dump_json(),
                    "capture_kind": "payload",
                }
            ]
        )
        assert patterns <= {(r["kind"], r["units"], r["digest"]) for r in rows}
    elif entry == "transition":
        await queue.mark_done(
            _stored_row(payload=json.dumps(request.model_dump())), written=0, ch=ch
        )
        assert len(ch.ended) == 1
    else:
        with pytest.raises(ValidationError, match="refused"):
            await queue.enqueue(request, scope=WHOLE_NAMESPACE, ch=ch)


async def test_erasure_records_an_indexed_refusal_for_a_derived_rows_own_identity(monkeypatch):
    from server.memory.store import refusals as policy

    monkeypatch.setattr(policy, "_writer", lambda: "process:test")
    row = policy.content_policy_row(
        {"org": "o", "namespace": "n", "user_id": "bob"}, "retired copper lark"
    )
    recorded = policy._recording_policy([row], scope_kind="user_id", scope_value="alice")
    assert {(r["scope_kind"], r["scope_value"], r["user_id"]) for r in recorded} == {
        ("user_id", "alice", "bob"),
        ("erased_identity", "n", "bob"),
    }


def test_queue_protocol_defaults_do_not_freeze_unrelated_intake():
    from server.memory.domain.refusals import payload_fingerprints, transcript_content

    old = {
        "messages": [{"role": "user", "content": "old passage"}],
        "record_schema": {"old_field": "string"},
        "pipeline_steps": [{"step_id": "loom.llm_extract"}],
    }
    patterns = payload_fingerprints(old)
    unrelated = {
        "messages": [{"role": "user", "content": "unrelated passage"}],
        "record_schema": {"new_field": "string"},
        "pipeline_steps": [{"step_id": "loom.llm_extract"}],
    }
    assert not matches(transcript_content(unrelated), patterns)
    for default in ("user", "string", "loom.llm_extract", "role", "content", "step_id"):
        assert not matches(default, patterns)


@pytest.mark.parametrize(
    "value", [4417, False, {"private": [4417, "copper lark"]}, [4417, "copper lark"]]
)
def test_typed_queue_arguments_are_checked_and_captured_as_values(value):
    from server.memory.domain.refusals import payload_fingerprints, transcript_content

    payload = {"pipeline_steps": [{"step_id": "loom.llm_extract", "config": {"arg": value}}]}
    assert matches(transcript_content(payload), fingerprint(value))
    assert set(fingerprint(value)) <= payload_fingerprints(payload)


async def test_a_refused_stale_queue_transition_cannot_stop_a_newer_source_revision():
    from server.core.hashing import sha256_text
    from server.memory.app import extraction_queue as queue
    from tests.unit.test_extraction_queue import _stored_row

    original = _stored_row(version=10)
    stale = queue._mirror(original, last_error="retired copper lark")
    current = {**original, "version": 15}

    class CH:
        async def execute(self, sql, params=None):
            assert "version={source_version:UInt64}" in sql
            assert "hex(SHA256(payload))={payload_sha:String}" in sql
            assert params["source_version"] == 10
            assert params["payload_sha"] == sha256_text(original["payload"]).upper()
            if current["version"] == params["source_version"]:
                current["status"] = "failed"

    await queue._drop_refused(CH(), stale)
    assert current["status"] == original["status"]


def test_known_pipeline_ids_are_classified_as_protocol():
    from server.memory.app.pipeline import registered_step_ids
    from server.memory.domain.refusals import EXTRACTION_PROTOCOL_STEPS

    assert EXTRACTION_PROTOCOL_STEPS == set(registered_step_ids())


@pytest.mark.parametrize(
    "step",
    [
        {"step_id": "retired copper lark"},
        {"step_id": "loom.hash_dedup", "retired copper lark": "ordinary"},
    ],
)
def test_unknown_step_ids_and_caller_argument_names_remain_content(step):
    from server.memory.domain.refusals import payload_fingerprints, transcript_content

    payload = {"pipeline_steps": [step]}
    patterns = set(fingerprint("retired copper lark"))
    assert matches(transcript_content(payload), patterns)
    assert patterns <= payload_fingerprints(payload)


@pytest.mark.parametrize("value", [None, False, 0, [], {}, ""])
@pytest.mark.parametrize("field", ["step_id", "config"])
def test_explicit_empty_and_typed_step_fields_are_not_protocol_defaults(field, value):
    from server.memory.domain.refusals import payload_fingerprints, transcript_content

    payload = {"pipeline_steps": [{field: value}]}
    assert matches(transcript_content(payload), fingerprint(value))
    assert set(fingerprint(value)) <= payload_fingerprints(payload)
