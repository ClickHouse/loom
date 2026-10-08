"""A session's outcome is labelled (client or judged) and a
failed session is contrasted with its nearest successful neighbour into one
reflection. Stub ClickHouse and LLM throughout."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime
from typing import Any

import pytest

from server.memory.app import session_outcomes as so
from server.memory.store.session_outcomes import OUTCOMES_TABLE


class FakeCH:
    def __init__(self) -> None:
        self.all_returns: list[list[dict[str, Any]]] = []
        self.one_returns: list[dict[str, Any] | None] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append(("all", " ".join(sql.split()), dict(params or {})))
        return self.all_returns.pop(0) if self.all_returns else []

    async def query_one(self, sql, params=None):
        self.calls.append(("one", " ".join(sql.split()), dict(params or {})))
        return self.one_returns.pop(0) if self.one_returns else None

    async def insert(self, table, rows, settings=None):
        self.inserted.append((table, rows))


class FakeLLM:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, user, max_tokens=1024, response_format=None):
        self.calls.append((system, user))
        text = self.answers.pop(0) if self.answers else "{}"
        if text == "RAISE":
            raise RuntimeError("provider down")

        class _R:
            pass

        r = _R()
        r.text = text
        return r


# --- parsing -----------------------------------------------------------------


def test_parse_judgement_reads_json_and_never_invents_a_label() -> None:
    assert so.parse_judgement('{"outcome":"positive","confidence":0.9,"reason":"Goal met."}') == (
        "positive",
        0.9,
        "Goal met.",
    )
    assert so.parse_judgement('```json\n{"outcome": "NEGATIVE", "confidence": "0.8"}\n```') == (
        "negative",
        0.8,
        "",
    )
    # unknown label, garbage, or unclear → unclear at zero confidence
    assert so.parse_judgement('{"outcome":"meh","confidence":0.9}') == ("unclear", 0.0, "")
    assert so.parse_judgement("not json") == ("unclear", 0.0, "")
    assert so.parse_judgement('{"outcome":"unclear","confidence":0.95}') == ("unclear", 0.0, "")
    assert so.parse_judgement('{"outcome":"positive","confidence":7}')[1] == 1.0


def test_parse_lessons_strips_numbering_dedups_and_caps_at_four() -> None:
    text = json.dumps(
        {
            "lessons": [
                "1. Check the object state before placing it.",
                "- check the object state before placing it",
                "2) Read the error before retrying.",
                "Ask for the file path.",
                "Fifth lesson.",
                "Sixth.",
            ],
            "what_failed": "Placed the egg   before cleaning it.",
        }
    )
    lessons, what_failed = so.parse_lessons(text)
    assert lessons == [
        "Check the object state before placing it.",
        "Read the error before retrying.",
        "Ask for the file path.",
        "Fifth lesson.",
    ]
    assert what_failed == "Placed the egg before cleaning it."
    assert so.parse_lessons("nonsense") == ([], "")
    assert so.parse_lessons('{"lessons": "a\\nb"}')[0] == ["a", "b"]


def test_build_outcome_row_validates_and_stamps_a_version() -> None:
    row = so.build_outcome_row(
        org="o",
        namespace="n",
        session_id="s",
        outcome="negative",
        confidence=1.7,
        reason="r",
        source="judged",
        now=datetime(2026, 9, 12, 8, 0, 0, 5),
    )
    assert row["confidence"] == 1.0 and row["judged_at"] == datetime(2026, 9, 12, 8, 0, 0)
    assert row["version"] > 0 and row["agent"] == "-" and row["run_id"] == "-"
    with pytest.raises(ValueError):
        so.build_outcome_row(
            org="o",
            namespace="n",
            session_id="s",
            outcome="maybe",
            confidence=1,
            reason="",
            source="judged",
        )
    with pytest.raises(ValueError):
        so.build_outcome_row(
            org="o",
            namespace="n",
            session_id="s",
            outcome="positive",
            confidence=1,
            reason="",
            source="oracle",
        )


# --- pass 1: label --------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_sessions_judges_unlabelled_summaries_and_isolates_failures() -> None:
    ch = FakeCH()
    ch.all_returns.append(
        [
            {
                "org": "o",
                "namespace": "n",
                "agent": "cc",
                "user_id": "alice",
                "session_id": "s1",
                "summary_text": "Fixed the flaky CI test.",
                "key_facts": ["pinned runner image"],
            },
            {
                "org": "o",
                "namespace": "n",
                "agent": "cc",
                "session_id": "s2",
                "summary_text": "Gave up on the migration.",
                "key_facts": [],
            },
            {
                "org": "o",
                "namespace": "n",
                "agent": "cc",
                "session_id": "s3",
                "summary_text": "Talked about lunch.",
                "key_facts": [],
            },
        ]
    )
    llm = FakeLLM(
        [
            '{"outcome":"positive","confidence":0.9,"reason":"Test fixed."}',
            "RAISE",
            '{"outcome":"unclear","confidence":0.2,"reason":""}',
        ]
    )
    cfg = so.DistillConfig(lookback_days=7, max_judged_per_pass=5)
    result = so.DistillResult()
    await so.label_sessions(ch, llm, cfg, result)
    assert result.sessions_scanned == 3 and result.labelled == 2
    assert result.by_outcome == {"positive": 1, "unclear": 1}
    assert len(result.failures) == 1 and "s2" in result.failures[0]
    kind, sql, params = ch.calls[0]
    assert (
        "LEFT ANTI JOIN" in sql
        and "loom.session_outcomes" in sql
        and params == {"days": 7, "lim": 5}
    )
    table, rows = ch.inserted[0]
    assert table == OUTCOMES_TABLE
    assert [(r["session_id"], r["outcome"], r["source"]) for r in rows] == [
        ("s1", "positive", "judged"),
        ("s3", "unclear", "judged"),
    ]
    assert (
        rows[0]["confidence"] == 0.9
        and rows[0]["reason"] == "Test fixed."
        and rows[0]["agent"] == "cc"
    )
    # the outcome is the summary's principal's: user_id is part of the table's key
    assert rows[0]["user_id"] == "alice" and rows[1]["user_id"] == "-"
    assert "o.user_id = s.user_id" in sql and "s.user_id AS user_id" in sql
    # the judge saw the digest and the key facts
    assert (
        "Fixed the flaky CI test." in llm.calls[0][1] and "pinned runner image" in llm.calls[0][1]
    )


@pytest.mark.asyncio
async def test_label_sessions_writes_nothing_when_everything_is_labelled() -> None:
    ch = FakeCH()
    result = so.DistillResult()
    await so.label_sessions(ch, FakeLLM([]), so.DistillConfig(), result)
    assert result.sessions_scanned == 0 and result.labelled == 0 and ch.inserted == []


# --- pass 2: contrast -----------------------------------------------------------


def _neg(sid="bad", conf=0.9):
    return {
        "org": "o",
        "namespace": "n",
        "agent": "cc",
        "session_id": sid,
        "reason": "Never ran the tests.",
        "confidence": conf,
        "summary_text": "Edited the parser, declared done.",
        "summary_embedding": [0.1, 0.2, 0.3],
        "embedding_model": "m",
        "user_id": "u",
    }


@pytest.mark.asyncio
async def test_distil_pairs_a_failed_session_with_its_nearest_success_and_writes_one_reflection() -> (
    None
):
    ch = FakeCH()
    ch.all_returns.append([_neg()])
    ch.one_returns.append(
        {
            "session_id": "good",
            "summary_text": "Edited the parser, ran the suite, fixed two.",
            "similarity": 0.8,
        }
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "lessons": ["Run the test suite before declaring a parser change done."],
                    "what_failed": "Declared done without running tests.",
                }
            )
        ]
    )
    written: list[dict[str, Any]] = []

    async def writer(**kw):
        written.append(kw)
        return "k"

    cfg = so.DistillConfig(
        min_confidence=0.7, min_pair_similarity=0.55, lookback_days=14, max_contrasts_per_pass=3
    )
    result = so.DistillResult()
    await so.distil_contrasts(ch, llm, cfg, result, writer=writer)
    assert (
        result.contrast_candidates == 1 and result.reflections_written == 1 and result.unpaired == 0
    )
    # the candidate query excludes sessions that already carry a contrast reflection
    kind, sql, params = ch.calls[0]
    # a summary and a reflection pair with the outcome by the session's identity,
    # not by the session id alone (two principals can share an id)
    assert "s.agent = o.agent" in sql and "r.user_id = s.user_id" in sql
    assert "s.user_id = o.user_id" in sql
    assert (
        "startsWith(memory_key, {prefix:String})" in sql
        and params["prefix"] == "reflection:contrast:"
    )
    assert (
        params["plen"] == len("reflection:contrast:")
        and params["minc"] == 0.7
        and params["lim"] == 3
    )
    # the neighbour query is namespace-scoped, same model, self excluded, positives only,
    # and the failed session's own user: another user's session never feeds these lessons
    kind, sql, params = ch.calls[1]
    assert "outcome = 'positive'" in sql and params["sid"] == "bad" and params["model"] == "m"
    assert "s.user_id = {uid:String}" in sql and params["uid"] == "u"
    assert "o.agent = s.agent" in sql and "o.user_id = s.user_id" in sql
    assert params["q"] == [0.1, 0.2, 0.3]
    # the contrast prompt carried both digests and the judged reason
    system, user = llm.calls[0]
    assert "Never ran the tests." in user and "ran the suite" in user and "declared done" in user
    w = written[0]
    assert (w["neg_session"], w["pos_session"]) == ("bad", "good")
    assert w["lessons"] == ["Run the test suite before declaring a parser change done."]
    assert w["what_failed"] == "Declared done without running tests."
    assert w["confidence"] == 0.8  # min(judge confidence 0.9, pair similarity 0.8)
    assert w["agent"] == "cc" and w["user_id"] == "u" and w["pair_similarity"] == 0.8


@pytest.mark.asyncio
async def test_distil_skips_when_no_neighbour_is_close_enough_or_nothing_was_learned() -> None:
    ch = FakeCH()
    ch.all_returns.append([_neg("a"), _neg("b"), _neg("c")])
    ch.one_returns.append(None)  # a: no positive session at all
    ch.one_returns.append(
        {"session_id": "far", "summary_text": "x", "similarity": 0.3}
    )  # b: below floor
    ch.one_returns.append(
        {"session_id": "good", "summary_text": "y", "similarity": 0.9}
    )  # c: no lessons
    llm = FakeLLM(['{"lessons": [], "what_failed": ""}'])
    written: list[dict[str, Any]] = []

    async def writer(**kw):
        written.append(kw)

    result = so.DistillResult()
    await so.distil_contrasts(ch, llm, so.DistillConfig(), result, writer=writer)
    assert result.contrast_candidates == 3 and result.unpaired == 3 and written == []
    assert len(llm.calls) == 1  # only the paired session cost a model call


@pytest.mark.asyncio
async def test_writer_lands_one_hidden_reflection_with_its_witnesses(monkeypatch) -> None:
    from server.memory.app import write as write_mod

    captured: list[Any] = []

    async def fake_set(inp):
        captured.append(inp)

    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    key = await so.write_contrast_reflection(
        org="o",
        namespace="n",
        agent="cc",
        user_id="u",
        neg_session="bad",
        pos_session="good",
        lessons=["Run the tests."],
        what_failed="No tests.",
        confidence=0.8,
        pair_similarity=0.81234,
    )
    assert key == "reflection:contrast:bad"
    inp = captured[0]
    assert inp.memory_type == "reflection" and inp.memory_key == key and inp.session_id == "bad"
    assert inp.value["supports"] == ["good"] and inp.value["contradicts"] == ["bad"]
    assert inp.value["lessons"] == ["Run the tests."] and inp.value["source"] == "contrast"
    assert inp.value["pair_similarity"] == 0.8123
    assert set(inp.tags) >= {
        "reflection",
        "contrast",
        "session:bad",
        "session:good",
        "status:failed",
    }
    assert inp.confidence == 0.8 and inp.source_trace == "contrast:bad<good"
    from server.memory.domain.memory_types import HIDDEN_FROM_UNFILTERED_SEARCH

    assert "reflection" in HIDDEN_FROM_UNFILTERED_SEARCH


# --- wiring -------------------------------------------------------------------


def test_erasure_deletes_session_outcomes_and_the_saga_includes_it(monkeypatch) -> None:
    from server.erasure.app import saga as erasure
    from server.erasure.store.steps_memory import delete_session_outcomes_by_namespace

    class _CH:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, Any]]] = []

        async def query_one(self, sql, params=None):
            return {"n": 1}

        async def execute(self, sql, params=None):
            self.executed.append((" ".join(sql.split()), dict(params or {})))

        async def query_all(self, sql, params=None):
            return [
                {
                    "mutation_id": "mutation_1.txt",
                    "is_done": 1,
                    "parts_to_do": 0,
                    "latest_fail_reason": "",
                }
            ]

    ch = _CH()
    assert asyncio.run(delete_session_outcomes_by_namespace("o", "ns", ch=ch)) == 1
    tables = [sql.split()[2] for sql, _ in ch.executed]
    assert tables == ["loom.session_outcomes_by_user"]
    sql, params = ch.executed[0]
    assert "DELETE WHERE" in sql and "mutations_sync = 0" in sql

    names: list[str] = []

    async def _fake_step(
        *, step_name: str, step_fn: Any, completed: set[str], row: dict[str, Any], **kw: Any
    ) -> Any:
        names.append(step_name)
        return None

    monkeypatch.setattr(erasure, "_do_step", _fake_step)
    monkeypatch.setattr("server.erasure.app.backends.all_backends", lambda: [])
    asyncio.run(erasure._saga_namespace("o", "ns", completed=set(), row={}))
    assert "session_outcomes.delete" in names
    assert names.index("session_outcomes.delete") > names.index("memory.tombstone")


def test_worker_is_default_off_and_registered() -> None:
    from server.settings import Settings
    from server.workers.registry import worker_meta

    s = Settings()
    assert s.reflection_distill_enabled is False
    assert "reflection_distill" in worker_meta()


def test_two_users_sharing_a_session_id_are_two_outcome_rows() -> None:
    """The outcomes table's ReplacingMergeTree key is the session's full
    identity and every writer stamps user_id."""
    from pathlib import Path

    from server.core.schema import table_rebuild as tr
    from server.core.schema.journal import split_statements

    root = Path(__file__).resolve().parents[2]
    sql = (root / "schemas/migrations/016_baseline.sql").read_text()
    [create] = [
        s
        for s in split_statements(sql)
        if s.startswith("CREATE TABLE IF NOT EXISTS " + OUTCOMES_TABLE + "\n")
    ]
    assert tr.layout_of(create).sorting_key == "org, namespace, agent, user_id, session_id"
    stmt = create
    a = so.build_outcome_row(
        org="o",
        namespace="n",
        agent="cc",
        user_id="alice",
        session_id="-",
        outcome="positive",
        confidence=1.0,
        reason="",
        source="client",
    )
    b = so.build_outcome_row(
        org="o",
        namespace="n",
        agent="cc",
        user_id="bob",
        session_id="-",
        outcome="negative",
        confidence=1.0,
        reason="",
        source="client",
    )
    key = [c.strip() for c in tr.layout_of(stmt).sorting_key.split(",")]
    assert tuple(a[c] for c in key) != tuple(b[c] for c in key)


def test_run_complete_labels_the_session_under_its_user() -> None:

    import server.tasks.app.run as run

    src = inspect.getsource(run._label_session)
    assert 'user_id=str(existing.get("user_id") or "-")' in src


def test_user_erasure_deletes_the_users_session_outcomes(monkeypatch) -> None:
    from server.erasure.app import saga_steps
    from server.erasure.store import steps_memory as memory_steps

    seen: list[tuple[str, str, dict]] = []

    async def _scoped(table, *, org, scope_where, params, ch, lightweight=False):
        seen.append((table, scope_where, params))
        return 1

    monkeypatch.setattr(memory_steps, "delete_scoped", _scoped)
    assert asyncio.run(memory_steps.delete_session_outcomes_by_user("o", "alice")) == 1
    assert seen == [(OUTCOMES_TABLE, "user_id = {uid:String}", {"uid": "alice"})]
    assert (
        dict(saga_steps.USER_STEPS)["session_outcomes.delete"]
        is memory_steps.delete_session_outcomes_by_user
    )
