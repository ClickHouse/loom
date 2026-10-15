"""Bi-temporal memory against a running server: which assertions applied at valid time
V according to what loom had recorded at knowledge time K, through corrections,
transitions, assessments, deletes, retries and erasure."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("bitemporal")
pytestmark = pytest.mark.asyncio


def jan(day: int) -> str:
    return datetime(2026, 1, day, tzinfo=UTC).isoformat()


async def _now_after(seconds: float = 0.05) -> str:
    """A knowledge time strictly between two commits."""
    await asyncio.sleep(seconds)
    t = datetime.now(UTC).isoformat()
    await asyncio.sleep(seconds)
    return t


async def _agent(control_plane_url: str, mint_test_token: Any, label: str) -> tuple[Any, str]:
    from loom import Agent

    ns = f"bt-{label}-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    return Agent(org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok), ns


async def _value_at(a: Any, key: str, v: str | None = None, k: str | None = None) -> Any:
    body: dict[str, Any] = {"memory_key": key}
    if v:
        body["valid_as_of"] = v
    if k:
        body["known_as_of"] = k
    out = await a.call("memory.get", body)
    return out["value"] if out["found"] else None


async def test_the_temporal_oracle(control_plane_url, mint_test_token) -> None:
    # Record A from January 1; later correct January 5–15 to B.
    a, _ = await _agent(control_plane_url, mint_test_token, "oracle")
    async with a:
        before = await _now_after()
        await a.call("memory.set", {"memory_key": "plan", "value": "A", "valid_at": jan(1)})
        k_a = await _now_after()
        out = await a.call(
            "memory.amend",
            {
                "memory_key": "plan",
                "change_kind": "correct",
                "value": "B",
                "valid_from": jan(5),
                "valid_to": jan(15),
            },
        )
        assert out["commit_seq"] == 2 and len(out["retired"]) == 1 and len(out["added"]) == 3
        assert await _value_at(a, "plan", jan(7), before) is None
        assert await _value_at(a, "plan", jan(7), k_a) == "A"
        assert await _value_at(a, "plan", jan(7)) == "B"
        assert await _value_at(a, "plan", jan(3)) == "A"
        assert await _value_at(a, "plan", jan(17)) == "A"
        # The current-state row follows what applies now.
        assert (await a.call("memory.get", {"memory_key": "plan"}))["value"] == "A"
        # A read with the amend's token sees it; coverage is disclosed.
        got = await a.call(
            "memory.get",
            {"memory_key": "plan", "valid_as_of": jan(7), "read_token": out["read_token"]},
        )
        assert got["value"] == "B" and got["temporal"]["cutoff_seq"] >= 2
        assert got["assertions"][0]["validity_basis"] == "stated"


async def test_a_transition_is_not_a_correction(control_plane_url, mint_test_token) -> None:
    # U01: a job change ends the old state at its effective time; fixing a
    # mistaken employer rewrites the belief and keeps the old one at earlier K.
    a, _ = await _agent(control_plane_url, mint_test_token, "u01")
    async with a:
        await a.call("memory.set", {"memory_key": "employer", "value": "Acme", "valid_at": jan(1)})
        await a.call(
            "memory.set",
            {
                "memory_key": "employer",
                "value": "Globex",
                "change_kind": "transition",
                "effective_at": jan(20),
            },
        )
        assert await _value_at(a, "employer", jan(10)) == "Acme"
        assert await _value_at(a, "employer", jan(25)) == "Globex"
        diff = await a.call("memory.temporal_diff", {"memory_key": "employer", "mode": "world"})
        kinds = [(c["kind"], c["assertion"]["value"]) for c in diff["changes"]]
        assert ("ended", "Acme") in kinds and ("began", "Globex") in kinds

        await a.call("memory.set", {"memory_key": "school", "value": "MIT", "valid_at": jan(1)})
        k = await _now_after()
        await a.call(
            "memory.amend",
            {
                "memory_key": "school",
                "change_kind": "correct",
                "value": "Stanford",
                "valid_from": jan(1),
            },
        )
        assert await _value_at(a, "school", jan(10)) == "Stanford"
        assert await _value_at(a, "school", jan(10), k) == "MIT"
        belief = await a.call(
            "memory.temporal_diff", {"memory_key": "school", "mode": "belief", "at": jan(10)}
        )
        assert [c["after"] for c in belief["changes"]][-1] == ["Stanford"]


async def test_a_repeated_value_is_a_new_episode_and_history_survives_delete(
    control_plane_url, mint_test_token
) -> None:
    # U04 + U22: A -> B -> A are three episodes; a delete removes the key from the
    # served set without claiming the world changed, and earlier K still answers.
    a, _ = await _agent(control_plane_url, mint_test_token, "u04")
    async with a:
        await a.call("memory.set", {"memory_key": "drink", "value": "tea", "valid_at": jan(1)})
        await a.call(
            "memory.set",
            {
                "memory_key": "drink",
                "value": "coffee",
                "change_kind": "transition",
                "effective_at": jan(10),
            },
        )
        await a.call(
            "memory.set",
            {
                "memory_key": "drink",
                "value": "tea",
                "change_kind": "transition",
                "effective_at": jan(20),
            },
        )
        iv = await a.call("memory.intervals", {"memory_key": "drink", "include_retired": True})
        served = [x["value"] for x in iv["assertions"]]
        assert served == ["tea", "coffee", "tea"]
        assert len({x["assertion_id"] for x in iv["assertions"]}) == 3
        assert iv["retired"]  # the open-ended states the transitions closed
        k = await _now_after()
        await a.call("memory.delete", {"memory_key": "drink"})
        assert await _value_at(a, "drink", jan(15)) is None
        assert await _value_at(a, "drink", jan(15), k) == "coffee"


async def test_assessments_modality_and_dispute_are_versioned(
    control_plane_url, mint_test_token
) -> None:
    # U08 / U14 / U06: a verification is visible from its commit; a plan stays a
    # plan; a disputed claim is flagged.
    a, _ = await _agent(control_plane_url, mint_test_token, "u14")
    async with a:
        await a.call(
            "memory.set",
            {
                "memory_key": "trip",
                "value": "Lisbon in May",
                "modality": "planned",
                "valid_at": jan(1),
            },
        )
        k = await _now_after()
        await a.call("memory.verify", {"memory_key": "trip"})
        now = await a.call("memory.get", {"memory_key": "trip", "valid_as_of": jan(2)})
        then = await a.call(
            "memory.get", {"memory_key": "trip", "valid_as_of": jan(2), "known_as_of": k}
        )
        assert now["assertions"][0]["modality"] == "planned"
        assert now["verified_by"] and not then["verified_by"]
        await a.call("memory.set", {"memory_key": "price", "value": "12", "modality": "disputed"})
        got = await a.call("memory.get", {"memory_key": "price", "valid_as_of": jan(2)})
        assert got["disputed"] is True


async def test_a_retry_reuses_its_commit_and_a_reused_key_is_refused(
    control_plane_url, mint_test_token
) -> None:
    # U23: the same Idempotency-Key replays the same commit; another body under it is refused.
    from loom.errors import LoomError

    a, _ = await _agent(control_plane_url, mint_test_token, "u23")
    async with a:
        key = f"retry-{uuid.uuid4().hex}"
        first = await a.call("memory.set", {"memory_key": "x", "value": "one"}, idempotency_key=key)
        again = await a.call("memory.set", {"memory_key": "x", "value": "one"}, idempotency_key=key)
        assert first["commit_seq"] == again["commit_seq"] and first["commit_seq"] > 0
        with pytest.raises(LoomError):
            await a.call("memory.set", {"memory_key": "x", "value": "two"}, idempotency_key=key)
        status = await a.call("memory.temporal_status", {})
        assert status["head_seq"] == first["commit_seq"]


async def test_a_read_ahead_of_the_journal_is_disclosed_not_answered(
    control_plane_url, mint_test_token
) -> None:
    # U25: a token naming a commit the journal has not reached waits its budget,
    # then answers incomplete with the gap named, never as if it covered it.
    from server.memory.domain.temporal_reads import encode_token

    a, ns = await _agent(control_plane_url, mint_test_token, "u25")
    async with a:
        w = await a.call("memory.set", {"memory_key": "k", "value": "v"})
        ahead = encode_token({ns: w["commit_seq"] + 100})
        got = await a.call("memory.get", {"memory_key": "k", "read_token": ahead})
        assert got["temporal"]["complete"] is False
        assert "read_token_not_ready" in got["temporal"]["gaps"]
        status = await a.call("memory.temporal_status", {})
        assert status["head_seq"] == w["commit_seq"] and status["write_blockers"] == []


async def test_search_at_an_earlier_knowledge_time_finds_the_old_belief(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "search")
    async with a:
        await a.call("memory.set", {"memory_key": "office", "value": "The office is in Berlin"})
        k = await _now_after()
        await a.call("memory.set", {"memory_key": "office", "value": "The office is in Munich"})
        now = await a.call(
            "memory.search",
            {
                "query": "where is the office",
                "known_as_of": None,
                "valid_as_of": datetime.now(UTC).isoformat(),
            },
        )
        then = await a.call("memory.search", {"query": "where is the office", "known_as_of": k})
        assert "Munich" in now["results"][0]["content_excerpt"]
        assert "Berlin" in then["results"][0]["content_excerpt"]
        assert then["results"][0]["assertion_id"]
        assert then["temporal"][0]["cutoff_seq"] == 1
        unified = await a.call("search.query", {"query": "where is the office", "known_as_of": k})
        assert unified["temporal_coverage"]["memory"] == "bitemporal"
        assert unified["knowledge"]["coverage"] == "not_historical"


async def test_a_namespace_erasure_removes_every_retained_revision(
    control_plane_url, mint_test_token
) -> None:
    # U22: erasure leaves no historical slice: not at an earlier K, not by id.
    from server.core.ch import get_client

    a, ns = await _agent(control_plane_url, mint_test_token, "erase")
    async with a:
        w = await a.call(
            "memory.set", {"memory_key": "secret", "value": "canary-" + uuid.uuid4().hex}
        )
        k = await _now_after()
        second = await a.call("memory.set", {"memory_key": "secret", "value": "second"})
        result = await a.erasure.request_namespace(ns, requester="ops@example.com")
        assert result["status"] == "completed"
        # The erased sequences are never handed out again: an old token or receipt
        # cannot come to name new content.
        after = await a.call("memory.set", {"memory_key": "fresh", "value": "after"})
        assert after["commit_seq"] > second["commit_seq"]
        # The projection restarted at the published head: the write projected inline.
        status = await a.call("memory.temporal_status", {})
        assert status["projection_seq"] == after["commit_seq"]
    ch = get_client()
    await ch.connect()
    try:
        for table in ("memory_temporal_commits", "memory_temporal_assertions"):
            row = await ch.query_one(
                f"SELECT count() AS n FROM loom.{table} WHERE org = {{o:String}} "
                "AND namespace = {n:String} AND memory_key = 'secret'",
                {"o": ORG, "n": ns},
            )
            assert int(row["n"]) == 0, table
    finally:
        await ch.disconnect()
    a2, _ = await _agent(control_plane_url, mint_test_token, "erase-read")
    async with a2:
        got = await a2.call("memory.get_by_id", {"memory_id": w["memory_id"], "known_as_of": k})
        assert got["found"] is False


async def test_another_tenant_cannot_read_history(control_plane_url, mint_test_token) -> None:
    a, ns = await _agent(control_plane_url, mint_test_token, "tenant")
    async with a:
        w = await a.call("memory.set", {"memory_key": "private", "value": "mine"})
        k = datetime.now(UTC).isoformat()
    from loom import Agent

    other = unique_org("bitemporal-other")
    tok = mint_test_token(org=other, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=other, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as b:
        got = await b.call("memory.get", {"memory_key": "private", "known_as_of": k})
        assert got["found"] is False
        by_id = await b.call("memory.get_by_id", {"memory_id": w["memory_id"], "known_as_of": k})
        assert by_id["found"] is False


async def test_graph_and_records_answer_at_a_knowledge_time(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "planes")
    async with a:
        k = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        listed = await a.call("facts.list", {"known_as_of": k})
        assert listed["rows"] == []
        q = await a.call("memory.record.query", {"schema_name": "none", "known_as_of": k})
        assert q["rows"] == []
        with pytest.raises(Exception):  # noqa: B017 — a future K is a 422
            await a.call(
                "memory.get",
                {
                    "memory_key": "x",
                    "known_as_of": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                },
            )


async def test_a_verify_after_a_correction_keeps_every_segment(
    control_plane_url, mint_test_token
) -> None:
    # A metadata write carries the head's value forward: it assesses the served
    # segments instead of replacing the corrected timeline with one interval.
    a, _ = await _agent(control_plane_url, mint_test_token, "keep")
    async with a:
        await a.call("memory.set", {"memory_key": "plan", "value": "A", "valid_at": jan(1)})
        await a.call(
            "memory.amend",
            {
                "memory_key": "plan",
                "change_kind": "correct",
                "value": "B",
                "valid_from": jan(5),
                "valid_to": jan(15),
            },
        )
        await a.call("memory.verify", {"memory_key": "plan"})
        iv = await a.call("memory.intervals", {"memory_key": "plan"})
        assert [x["value"] for x in iv["assertions"]] == ["A", "B", "A"]
        # The attestation binds A's content: B's segment is not verified by it.
        assert [bool(x["verified_by"]) for x in iv["assertions"]] == [True, False, True]
        assert await _value_at(a, "plan", jan(7)) == "B"
        hits = await a.call("memory.search", {"query": "B", "valid_as_of": jan(7)})
        assert [h["verified"] for h in hits["results"] if h["memory_key"] == "plan"] == [False]


async def test_the_shared_history_mirror_keeps_every_version() -> None:
    # shared_versions_mv keeps each shared version after the ReplacingMergeTree head
    # merges it away.
    import os

    os.environ.get("LOOM_CH_HOST") or pytest.skip("needs ClickHouse")
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    ns = f"bt-mirror-{uuid.uuid4().hex[:6]}"
    try:
        for v in (1, 2):
            await ch.insert(
                "loom.shared",
                [
                    {
                        "org": ORG,
                        "namespace": ns,
                        "memory_key": "k",
                        "value": f"v{v}",
                        "memory_type": "semantic",
                        "confidence": 1.0,
                        "promoted_from": [],
                        "promotion_event": str(uuid.uuid4()),
                        "promoted_by": "t",
                        "reversible_until": datetime(2030, 1, 1),
                        "superseded_by": None,
                        "tombstoned_at": None,
                        "version": v,
                    }
                ],
            )
        await ch.execute("OPTIMIZE TABLE loom.shared FINAL")
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.shared_versions WHERE org = {o:String} AND namespace = {n:String}",
            {"o": ORG, "n": ns},
        )
        assert int(row["n"]) == 2
    finally:
        await ch.disconnect()


async def test_the_next_write_repairs_an_integrity_break(
    control_plane_url, mint_test_token
) -> None:
    # Two payloads at one sequence (a withdrawal that failed): the next write keeps
    # the first-landed commit, deletes the other and publishes.
    import os

    os.environ.get("LOOM_CH_HOST") or pytest.skip("needs ClickHouse")
    from server.core.ch import get_client

    a, ns = await _agent(control_plane_url, mint_test_token, "repair")
    async with a:
        first = await a.call("memory.set", {"memory_key": "k", "value": "kept"})
        ch = get_client()
        await ch.connect()
        try:
            row = await ch.query_one(
                "SELECT * EXCEPT inserted_at FROM loom.memory_temporal_commits "
                "WHERE org = {o:String} AND namespace = {n:String}",
                {"o": ORG, "n": ns},
            )
            row = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in row.items()}
            row.update(commit_id=str(uuid.uuid4()), payload_sha256="f" * 64)
            await ch.insert("loom.memory_temporal_commits", [row])
            status = await a.call("memory.temporal_status", {})
            assert status["integrity_break_at"] == first["commit_seq"]
            await a.call("memory.set", {"memory_key": "k2", "value": "x"})
            left = await ch.query_one(
                "SELECT count() AS n FROM loom.memory_temporal_commits "
                "WHERE org = {o:String} AND namespace = {n:String} AND commit_seq = {s:UInt64}",
                {"o": ORG, "n": ns, "s": first["commit_seq"]},
            )
            assert int(left["n"]) == 1
        finally:
            await ch.disconnect()
        assert (await a.call("memory.temporal_status", {}))["integrity_break_at"] == 0
        assert await _value_at(a, "k") == "kept"


async def test_clear_and_reject_are_journaled_retractions(
    control_plane_url, mint_test_token
) -> None:
    # The in-place mutations take rows out of service; the journal records that
    # first, so an earlier K still sees them and now does not.
    a, _ = await _agent(control_plane_url, mint_test_token, "clear")
    async with a:
        await a.call("memory.set", {"memory_key": "c1", "value": "one", "user_id": "u1"})
        await a.call("memory.set", {"memory_key": "r1", "value": "two"})
        k = await _now_after()
        await a.call("memory.clear", {"user_id": "u1", "confirm_subject": "u1"})
        await a.call("memory.reject", {"memory_key": "r1", "rejection_reason": "wrong"})
        assert await _value_at(a, "r1", jan(1)) is None
        assert await _value_at(a, "r1", jan(1), k) == "two"
        cleared = await a.call(
            "memory.get", {"memory_key": "c1", "user_id": "u1", "known_as_of": k}
        )
        assert cleared["value"] == "one"
        gone = await a.call(
            "memory.get",
            {"memory_key": "c1", "user_id": "u1", "valid_as_of": datetime.now(UTC).isoformat()},
        )
        assert gone["found"] is False


async def test_redaction_and_blacklist_reach_every_retained_slice(
    control_plane_url, mint_test_token
) -> None:
    # A redacted version and a blacklisted value are gone at every knowledge time,
    # not only from the head.
    a, _ = await _agent(control_plane_url, mint_test_token, "scrub")
    async with a:
        first = await a.call("memory.set", {"memory_key": "k", "value": "SECRET-123"})
        k1 = await _now_after()
        await a.call("memory.set", {"memory_key": "k", "value": "public"})
        red = await a.call(
            "memory.redact", {"memory_id": first["memory_id"], "version": first["version"]}
        )
        assert red["redacted"] is True
        then = await a.call("memory.get", {"memory_key": "k", "known_as_of": k1})
        assert then["value"] == "[REDACTED]"
        iv = await a.call("memory.intervals", {"memory_key": "k", "include_retired": True})
        assert "SECRET-123" not in str(iv)

        await a.call("memory.set", {"memory_key": "tok", "value": "sk-LEAK-999"})
        k2 = await _now_after()
        await a.call("blacklist.add", {"pattern": "sk-LEAK", "scope": "namespace", "sweep": True})
        got = await a.call("memory.get", {"memory_key": "tok", "known_as_of": k2})
        assert got["found"] is False
        hits = await a.call("memory.search", {"query": "sk-LEAK-999", "known_as_of": k2})
        assert all("sk-LEAK" not in h["content_excerpt"] for h in hits["results"])


async def test_redaction_reaches_text_only_the_journal_holds(
    control_plane_url, mint_test_token
) -> None:
    # An amend's value never was a loom.memory version of its own: it is redacted by
    # its assertion id, and a version redact still scrubs the amend that restated it.
    a, _ = await _agent(control_plane_url, mint_test_token, "scrub-amend")
    async with a:
        await a.call(
            "memory.amend",
            {
                "memory_key": "k",
                "change_kind": "transition",
                "value": "AMEND-SECRET",
                "effective_at": jan(1),
            },
        )
        await a.call("memory.set", {"memory_key": "k", "value": "public"})
        iv = await a.call("memory.intervals", {"memory_key": "k", "include_retired": True})
        old = next(x for x in iv["retired"] if "AMEND-SECRET" in str(x["value"]))
        served = iv["assertions"][0]
        mid = (await a.call("memory.get", {"memory_key": "k"}))["memory_id"]
        refused = await a.call(
            "memory.redact", {"memory_id": mid, "assertion_id": served["assertion_id"]}
        )
        assert refused["redacted"] is False and refused["refused_head"] is True
        red = await a.call("memory.redact", {"memory_id": mid, "assertion_id": old["assertion_id"]})
        assert red["redacted"] is True
        iv = await a.call("memory.intervals", {"memory_key": "k", "include_retired": True})
        assert "AMEND-SECRET" not in str(iv)
        # Every copy goes, not only the journal's: the amend's current-state version too.
        hist = await a.call("memory.history", {"memory_key": "k"})
        assert "AMEND-SECRET" not in str(hist)
        assert await _value_at(a, "k") == "public"

        # Journal-only text stays redactable once the key is deleted.
        await a.call("memory.set", {"memory_key": "gone", "value": "plain"})
        span = {"valid_from": jan(1), "valid_to": jan(5)}
        await a.call(
            "memory.amend",
            {"memory_key": "gone", "change_kind": "correct", "value": "JOURNAL-ONLY", **span},
        )
        k_mid = await _now_after()
        await a.call(
            "memory.amend",
            {"memory_key": "gone", "change_kind": "correct", "value": "plain", **span},
        )
        iv = await a.call("memory.intervals", {"memory_key": "gone", "include_retired": True})
        j = next(x for x in iv["retired"] if "JOURNAL-ONLY" in str(x["value"]))
        gone_mid = (await a.call("memory.get", {"memory_key": "gone"}))["memory_id"]
        await a.call("memory.delete", {"memory_key": "gone"})
        red = await a.call(
            "memory.redact", {"memory_id": gone_mid, "assertion_id": j["assertion_id"]}
        )
        assert red["redacted"] is True
        then = await a.call(
            "memory.get", {"memory_key": "gone", "known_as_of": k_mid, "valid_as_of": jan(2)}
        )
        assert "JOURNAL-ONLY" not in str(then)

        # A locked key's evidence is refused by either address.
        await a.call("memory.set", {"memory_key": "ev", "value": "EVIDENCE-1"})
        await a.call("memory.set", {"memory_key": "ev", "value": "current"})
        await a.call("memory.lock", {"memory_key": "ev", "mode": "no_delete", "reason": "hold"})
        iv = await a.call("memory.intervals", {"memory_key": "ev", "include_retired": True})
        ev = next(x for x in iv["retired"] if "EVIDENCE-1" in str(x["value"]))
        ev_mid = (await a.call("memory.get", {"memory_key": "ev"}))["memory_id"]
        with pytest.raises(Exception, match="LOCKED|locked"):
            await a.call("memory.redact", {"memory_id": ev_mid, "assertion_id": ev["assertion_id"]})


async def test_a_historical_list_applies_the_live_filters(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "list-filters")
    async with a:
        await a.call("memory.set", {"memory_key": "deploy", "value": "Friday window " * 20})
        await a.call("memory.set", {"memory_key": "other", "value": "lunch"})
        await a.call("memory.set", {"memory_key": "mine", "value": "deploy too", "user_id": "u1"})
        k = await _now_after()
        listed = await a.call(
            "memory.list", {"known_as_of": k, "contains": "FRIDAY", "preview_chars": 40}
        )
        assert [e["memory_key"] for e in listed["entries"]] == ["deploy"]
        assert listed["entries"][0]["value_truncated"] is True
        live = await a.call("memory.list", {"contains": "FRIDAY", "preview_chars": 40})
        assert listed["entries"][0]["value"] == live["entries"][0]["value"]
        # The default identity's user is '-': u1's row is another user's.
        everyone = await a.call("memory.list", {"known_as_of": k, "user_id": "*"})
        assert {e["memory_key"] for e in everyone["entries"]} == {"deploy", "other", "mine"}
        mine = await a.call("memory.list", {"known_as_of": k})
        assert {e["memory_key"] for e in mine["entries"]} == {"deploy", "other"}
        with pytest.raises(Exception, match="sort_by=score"):
            await a.call("memory.list", {"known_as_of": k, "sort_by": "score"})


async def test_concurrent_writes_to_one_namespace_all_publish(
    control_plane_url, mint_test_token
) -> None:
    # The lock covers only stamping and the insert, so independent keys do not
    # queue behind each other's reads.
    a, _ = await _agent(control_plane_url, mint_test_token, "burst")
    async with a:
        outs = await asyncio.gather(
            *(a.call("memory.set", {"memory_key": f"k{i}", "value": f"v{i}"}) for i in range(30))
        )
        seqs = sorted(o["commit_seq"] for o in outs)
        assert seqs == list(range(1, 31))
        status = await a.call("memory.temporal_status", {})
        assert status["integrity_break_at"] == 0 and status["head_seq"] == 30


async def test_list_amend_and_search_follow_the_temporal_contract(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "contract")
    async with a:
        await a.call("memory.set", {"memory_key": "early", "value": "first fact"})
        k = await _now_after()
        await a.call("memory.set", {"memory_key": "late", "value": "second fact"})
        listed = await a.call("memory.list", {"known_as_of": k})
        assert [e["memory_key"] for e in listed["entries"]] == ["early"]

        # A new key amended into existence has one memory id, journal and head alike.
        await a.call(
            "memory.amend",
            {
                "memory_key": "plan",
                "change_kind": "transition",
                "value": "move",
                "effective_at": jan(1),
            },
        )
        head = await a.call("memory.get", {"memory_key": "plan"})
        then = await a.call("memory.get", {"memory_key": "plan", "valid_as_of": jan(2)})
        assert head["memory_id"] == then["memory_id"]
        by_id = await a.call(
            "memory.get_by_id", {"memory_id": head["memory_id"], "valid_as_of": jan(2)}
        )
        assert by_id["found"] is True

        # Nothing close to the query: the historical search abstains like a current one.
        miss = await a.call("memory.search", {"query": "zebra unicorn", "known_as_of": k})
        assert miss["abstain"] is True


async def test_a_high_sensitivity_key_reads_its_own_history_only_by_key(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "high")
    async with a:
        await a.call("memory.set", {"memory_key": "h", "value": "HIGH-1", "sensitivity": "high"})
        await a.call("memory.set", {"memory_key": "h", "value": "HIGH-2", "sensitivity": "high"})
        search = await a.call("memory.search", {"query": "HIGH", "valid_as_of": jan(1)})
        assert all(r["memory_key"] != "h" for r in search["results"])
        iv = await a.call("memory.intervals", {"memory_key": "h", "include_retired": True})
        assert [x["value"] for x in iv["assertions"]] == ["HIGH-2"]
        assert [x["value"] for x in iv["retired"]] == ["HIGH-1"]


async def _ch() -> Any:
    import os

    os.environ.get("LOOM_CH_HOST") or pytest.skip("needs ClickHouse")
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    return ch


async def test_a_key_made_high_withholds_its_earlier_revisions(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "made-high")
    async with a:
        await a.call("memory.set", {"memory_key": "p", "value": "launch codename aurora"})
        k = await _now_after()
        await a.call(
            "memory.set",
            {"memory_key": "p", "value": "launch codename aurora", "sensitivity": "high"},
        )
        then = await a.call("memory.search", {"query": "launch codename aurora", "known_as_of": k})
        assert all(r["memory_key"] != "p" for r in then["results"])
        listed = await a.call("memory.list", {"known_as_of": k})
        assert all(e["memory_key"] != "p" for e in listed["entries"])


async def test_historical_search_keeps_the_session_filters(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "sessions")
    async with a:
        for sid, hour in (("s1", "ten"), ("s2", "eleven")):
            await a.call(
                "memory.set",
                {
                    "memory_key": f"note-{sid}",
                    "value": f"standup moved to {hour}",
                    "session_id": sid,
                },
            )
        k = await _now_after()
        one = await a.call(
            "memory.search", {"query": "standup moved", "known_as_of": k, "session_id": "s1"}
        )
        assert {r["memory_key"] for r in one["results"]} == {"note-s1"}
        # The SDK's identity sends session_id '-'; '*' lifts it so the set decides.
        some = await a.call(
            "memory.search",
            {"query": "standup moved", "known_as_of": k, "session_id": "*", "session_ids": ["s2"]},
        )
        assert {r["memory_key"] for r in some["results"]} == {"note-s2"}
        with pytest.raises(Exception, match="run_id"):
            await a.call("memory.search", {"query": "x", "known_as_of": k, "run_id": "r1"})


async def test_temporal_reads_agree_with_the_current_contract(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "contract")
    async with a:
        w = await a.call("memory.set", {"memory_key": "j", "value": {"b": 2, "a": 1}})
        live = await a.call("memory.get", {"memory_key": "j"})
        then = await a.call("memory.get", {"memory_key": "j", "read_token": w["read_token"]})
        assert then["content_sha256"] == live["content_sha256"]
        # A strict policy alone is a temporal read: an undated claim is not admitted.
        strict = await a.call("memory.get", {"memory_key": "j", "validity_policy": "strict"})
        assert strict["found"] is False and strict["temporal"] is not None
        # Same value, newly declared as asserted: a revision, not an assessment.
        await a.call("memory.set", {"memory_key": "trip", "value": "Lisbon", "modality": "planned"})
        await a.call(
            "memory.set", {"memory_key": "trip", "value": "Lisbon", "modality": "asserted"}
        )
        got = await a.call("memory.get", {"memory_key": "trip", "valid_as_of": jan(1)})
        assert [x["modality"] for x in got["assertions"]] == ["asserted"]
        # A verify is a belief change of its own.
        await a.call("memory.verify", {"memory_key": "trip"})
        diff = await a.call("memory.temporal_diff", {"memory_key": "trip", "mode": "belief"})
        last = diff["changes"][-1]
        assert last["kind"] == "assess" and last["after_assertions"][0]["verified_by"]


async def test_get_by_id_resolves_through_the_journal(control_plane_url, mint_test_token) -> None:
    a, ns = await _agent(control_plane_url, mint_test_token, "byid")
    async with a:
        w = await a.call("memory.set", {"memory_key": "k", "value": "v"})
        mid = (await a.call("memory.get", {"memory_key": "k"}))["memory_id"]
        ch = await _ch()
        try:
            # The projection lost the key and its watermark: the journal still answers.
            for table in ("memory_temporal_assertions", "memory_temporal_watermarks"):
                await ch.execute(
                    f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                    params={"o": ORG, "n": ns},
                    settings={"lightweight_deletes_sync": 2},
                )
        finally:
            await ch.disconnect()
        got = await a.call("memory.get_by_id", {"memory_id": mid, "read_token": w["read_token"]})
        assert got["found"] is True and got["value"] == "v"


async def test_get_by_id_never_answers_with_a_later_memory_of_the_key(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "byid-new")
    async with a:
        await a.call("memory.set", {"memory_key": "k", "value": "first"})
        old = (await a.call("memory.get", {"memory_key": "k"}))["memory_id"]
        k = await _now_after()
        await a.call("memory.delete", {"memory_key": "k"})
        await a.call("memory.set", {"memory_key": "k", "value": "second"})
        now = datetime.now(UTC).isoformat()
        got = await a.call("memory.get_by_id", {"memory_id": old, "valid_as_of": now})
        assert got["found"] is False  # the key's new memory is another id
        then = await a.call("memory.get_by_id", {"memory_id": old, "known_as_of": k})
        assert then["found"] is True and then["value"] == "first"


async def test_memory_set_writes_only_what_applies_now(control_plane_url, mint_test_token) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "now-only")
    async with a:
        await a.call("memory.set", {"memory_key": "employer", "value": "Acme", "valid_at": jan(1)})
        later = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        with pytest.raises(Exception, match="memory.amend"):
            await a.call(
                "memory.set",
                {
                    "memory_key": "employer",
                    "value": "Globex",
                    "change_kind": "transition",
                    "effective_at": later,
                },
            )
        with pytest.raises(Exception, match="memory.amend"):
            await a.call(
                "memory.set",
                {
                    "memory_key": "employer",
                    "value": "Initech",
                    "change_kind": "correct",
                    "valid_at": jan(2),
                    "valid_to": jan(3),
                },
            )
        await a.call(
            "memory.amend",
            {
                "memory_key": "employer",
                "change_kind": "transition",
                "value": "Globex",
                "effective_at": later,
            },
        )
        assert (await a.call("memory.get", {"memory_key": "employer"}))["value"] == "Acme"
        assert await _value_at(a, "employer", later) == "Globex"


async def test_the_reconciler_brings_the_current_row_in_line(
    control_plane_url, mint_test_token
) -> None:
    # A row the journal committed but loom.memory lost, and a scheduled transition
    # whose time has come: the projection worker rewrites both to what applies now.
    from server.memory.app import temporal_projection as tp

    a, ns = await _agent(control_plane_url, mint_test_token, "reconcile")
    async with a:
        await a.call("memory.set", {"memory_key": "lost", "value": "kept in the journal"})
        await a.call("memory.set", {"memory_key": "pin", "value": "keep me"})
        pinned = await a.call("memory.pin", {"memory_key": "pin", "value": "keep me"})
        await a.call("memory.set", {"memory_key": "job", "value": "Acme", "valid_at": jan(1)})
        soon = datetime.now(UTC) + timedelta(seconds=2)
        await a.call(
            "memory.amend",
            {
                "memory_key": "job",
                "change_kind": "transition",
                "value": "Globex",
                "effective_at": soon.isoformat(),
            },
        )
        ch = await _ch()
        try:
            await ch.execute(
                "DELETE FROM loom.memory WHERE org = {o:String} AND namespace = {n:String} "
                "AND memory_key = 'lost'",
                params={"o": ORG, "n": ns},
                settings={"lightweight_deletes_sync": 2},
            )
            assert (await a.call("memory.get", {"memory_key": "lost"}))["found"] is False
            # The pin was journaled; its current row was not written.
            await ch.execute(
                "DELETE FROM loom.memory WHERE org = {o:String} AND namespace = {n:String} "
                "AND memory_key = 'pin' AND pinned = 1",
                params={"o": ORG, "n": ns},
                settings={"lightweight_deletes_sync": 2},
            )
            assert (await a.call("memory.get", {"memory_key": "pin"}))["pinned"] is False
            await asyncio.sleep(max(0.0, (soon - datetime.now(UTC)).total_seconds()) + 0.5)
            # The cursors page through whatever earlier tests left; a few passes.
            for _ in range(50):
                await tp.reconcile_current(settle_s=0, only=(ORG, ns))
                lost = await a.call("memory.get", {"memory_key": "lost"})
                job = await a.call("memory.get", {"memory_key": "job"})
                pin = await a.call("memory.get", {"memory_key": "pin"})
                if lost["found"] and job["value"] == "Globex" and pin["pinned"]:
                    break
        finally:
            await ch.disconnect()
        assert lost["value"] == "kept in the journal"
        assert job["value"] == "Globex"
        assert pin["pinned"] is True and pinned


async def test_a_historical_ask_reads_only_the_journal_at_its_cutoff(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "ask-k")
    async with a:
        await a.call("memory.set", {"memory_key": "ticket-41", "value": "ticket 41 is open"})
        k = await _now_after()
        await a.call("memory.set", {"memory_key": "ticket-42", "value": "ticket 42 is closed"})
        out = await a.call(
            "memory.ask",
            {
                "query": "what is the status of ticket 42?",
                "known_as_of": k,
                "allow_synthesis": False,
            },
        )
        assert all(r["memory_key"] != "ticket-42" for r in out["results"])
        assert out["temporal"] and out["temporal"][0]["known_as_of"]


async def test_todays_fence_holds_while_the_projection_lags(
    control_plane_url, mint_test_token
) -> None:
    a, ns = await _agent(control_plane_url, mint_test_token, "lag-high")
    async with a:
        await a.call("memory.set", {"memory_key": "p", "value": "badge code orchid"})
        k = await _now_after()
        await a.call(
            "memory.set", {"memory_key": "p", "value": "badge code orchid", "sensitivity": "high"}
        )
        ch = await _ch()
        try:
            # The 'high' revision never reached the projection.
            await ch.execute(
                "DELETE FROM loom.memory_temporal_assertions WHERE org = {o:String} "
                "AND namespace = {n:String} AND sensitivity = 'high'",
                params={"o": ORG, "n": ns},
                settings={"lightweight_deletes_sync": 2},
            )
        finally:
            await ch.disconnect()
        then = await a.call("memory.search", {"query": "badge code orchid", "known_as_of": k})
        assert all(r["memory_key"] != "p" for r in then["results"])


async def test_historical_reads_match_the_current_contract_details(
    control_plane_url, mint_test_token
) -> None:
    a, ns = await _agent(control_plane_url, mint_test_token, "details")
    async with a:
        # Verification is derived the same way on both paths.
        await a.call("memory.set", {"memory_key": "v", "value": "checked"})
        await a.call("memory.verify", {"memory_key": "v"})
        live = await a.call("memory.get", {"memory_key": "v"})
        then = await a.call("memory.get", {"memory_key": "v", "valid_as_of": jan(1)})
        assert then["verification"] == live["verification"] != "none"
        # Recency is the list's order, before the page is cut.
        await a.call("memory.set", {"memory_key": "a-old", "value": "first"})
        await a.call("memory.set", {"memory_key": "z-new", "value": "second"})
        k = await _now_after()
        page = await a.call("memory.list", {"known_as_of": k, "limit": 1})
        assert [e["memory_key"] for e in page["entries"]] == ["z-new"]
        # Equal values under other qualifiers, one dated and one not, diff cleanly.
        await a.call(
            "memory.amend",
            {"memory_key": "q", "change_kind": "correct", "value": "A", "qualifiers": {"r": "x"}},
        )
        await a.call(
            "memory.amend",
            {
                "memory_key": "q",
                "change_kind": "correct",
                "value": "A",
                "valid_from": jan(1),
                "qualifiers": {"r": "y"},
            },
        )
        diff = await a.call("memory.temporal_diff", {"memory_key": "q", "mode": "belief"})
        assert diff["changes"]
    ch = await _ch()
    try:
        await asyncio.sleep(1)
        row = await ch.query_one(
            "SELECT returned_ids, cutoff_seq FROM loom.memory_exposures WHERE org = {o:String} "
            "AND namespace = {n:String} AND operation = 'memory.list' ORDER BY ts DESC LIMIT 1",
            {"o": ORG, "n": ns},
        )
    finally:
        await ch.disconnect()
    assert row and len(row["returned_ids"]) == 1 and int(row["cutoff_seq"]) > 0


async def test_transitions_due_at_one_instant_all_reach_the_current_row(
    control_plane_url, mint_test_token
) -> None:
    # More keys than one reconciler page, all scheduled for the same instant.
    from server.memory.app import temporal_projection as tp

    a, _ = await _agent(control_plane_url, mint_test_token, "same-instant")
    async with a:
        at = (datetime.now(UTC) + timedelta(seconds=3)).replace(microsecond=0)
        keys = [f"k{i}" for i in range(8)]
        for k in keys:
            await a.call("memory.set", {"memory_key": k, "value": "old", "valid_at": jan(1)})
            await a.call(
                "memory.amend",
                {
                    "memory_key": k,
                    "change_kind": "transition",
                    "value": "new",
                    "effective_at": at.isoformat(),
                },
            )
        await asyncio.sleep(max(0.0, (at - datetime.now(UTC)).total_seconds()) + 0.5)
        ch = await _ch()
        try:
            for _ in range(60):
                await tp.reconcile_current(limit=3, settle_s=0)
                got = [(await a.call("memory.get", {"memory_key": k}))["value"] for k in keys]
                if got == ["new"] * len(keys):
                    break
        finally:
            await ch.disconnect()
        assert got == ["new"] * len(keys)


async def test_a_held_namespace_never_holds_back_another_tenant(
    control_plane_url, mint_test_token
) -> None:
    from server.memory.app import temporal_projection as tp

    h, ns_h = await _agent(control_plane_url, mint_test_token, "held")
    o, _ = await _agent(control_plane_url, mint_test_token, "other")

    async def schedule(a: Any, key: str, at: datetime) -> None:
        await a.call(
            "memory.amend",
            {
                "memory_key": key,
                "change_kind": "transition",
                "value": "new",
                "effective_at": at.isoformat(),
            },
        )

    async with h, o:
        keys = [f"k{i}" for i in range(4)]
        for a, k in [(h, "k"), *((o, k) for k in keys)]:
            await a.call("memory.set", {"memory_key": k, "value": "old", "valid_at": jan(1)})
        # Only the held key's transition must still be ahead when it is amended (the
        # hold must come after its amend, and the background workers are off, so
        # only this test's passes apply it). The other tenant's come later; one that
        # is already due when amended applies at once, which the test expects anyway.
        start = (datetime.now(UTC) + timedelta(seconds=3)).replace(microsecond=0)
        await schedule(h, "k", start)
        ch = await _ch()
        eid = str(uuid.uuid4())
        try:
            await ch.insert(
                "loom.erasure_requests",
                [
                    {
                        "id": eid,
                        "org": ORG,
                        "scope_type": "namespace",
                        "scope_value": ns_h,
                        "requester": "test",
                        "status": "failed",
                        "failure_reason": "a step failed; retrying",
                        "version": 1,
                    }
                ],
            )
            for i, k in enumerate(keys):
                await schedule(o, k, start + timedelta(seconds=i + 1))
            await asyncio.sleep(max(0.0, (start - datetime.now(UTC)).total_seconds()) + 5.5)
            for _ in range(40):
                await tp.reconcile_current(limit=2, settle_s=0)
                got = [(await o.call("memory.get", {"memory_key": k}))["value"] for k in keys]
                if got == ["new"] * len(keys):
                    break
            assert got == ["new"] * len(keys)
            assert (await h.call("memory.get", {"memory_key": "k"}))["value"] == "old"
            # The hold lifts (here: the injected request goes away; a real erasure
            # would have removed the content): the namespace catches up from its place.
            await _delete(ch, "DELETE FROM loom.erasure_requests WHERE id = {i:UUID}", i=eid)
            for _ in range(40):
                await tp.reconcile_current(limit=2, settle_s=0)
                if (await h.call("memory.get", {"memory_key": "k"}))["value"] == "new":
                    break
            assert (await h.call("memory.get", {"memory_key": "k"}))["value"] == "new"
        finally:
            await ch.disconnect()


async def test_a_held_namespace_keeps_its_earliest_boundary_through_a_merge() -> None:
    from server.memory.store import temporal as store

    ch = await _ch()
    ns = f"bt-merge-{uuid.uuid4().hex[:6]}"
    try:
        await store.hold_boundary(ORG, ns, 1000, ch=ch)
        await store.hold_boundary(ORG, ns, 5000, ch=ch)
        await ch.execute("OPTIMIZE TABLE loom.memory_temporal_watermarks FINAL")
        rows = [r for r in await store.pending_boundaries(ch=ch) if r["namespace"] == ns]
        assert [int(r["since"]) for r in rows] == [1000]
        await store.hold_boundary(ORG, ns, 500, ch=ch)
        await ch.execute("OPTIMIZE TABLE loom.memory_temporal_watermarks FINAL")
        rows = [r for r in await store.pending_boundaries(ch=ch) if r["namespace"] == ns]
        assert [int(r["since"]) for r in rows] == [500]
    finally:
        await store.release_boundary(ORG, ns, ch=ch)
        await ch.disconnect()


async def _delete(ch: Any, sql: str, **params: Any) -> None:
    await ch.execute(sql, params=params, settings={"lightweight_deletes_sync": 2})


async def test_an_erasure_cut_hides_what_cleanup_left_behind(
    control_plane_url, mint_test_token
) -> None:
    # Whatever an overtaken or failed cleanup left in the journal or projection,
    # content recorded before an erasure of its scope is never served.
    a, ns = await _agent(control_plane_url, mint_test_token, "cut")
    async with a:
        await a.call("memory.set", {"memory_key": "p", "value": "private", "user_id": "u-cut"})
        before = await a.call(
            "memory.get", {"memory_key": "p", "user_id": "u-cut", "valid_as_of": jan(1)}
        )
        assert before["found"] is True
        ch = await _ch()
        try:
            await ch.insert(
                "loom.erasure_requests",
                [
                    {
                        "id": str(uuid.uuid4()),
                        "org": ORG,
                        "scope_type": "user_id",
                        "scope_value": "u-cut",
                        "requester": "test",
                        "status": "completed",
                        "version": 1,
                    }
                ],
            )
        finally:
            await ch.disconnect()
        for args in ({"valid_as_of": jan(1)}, {"known_as_of": datetime.now(UTC).isoformat()}):
            got = await a.call("memory.get", {"memory_key": "p", "user_id": "u-cut", **args})
            assert got["found"] is False
        await asyncio.sleep(2.1)  # past the request's (whole) second
        await a.call("memory.set", {"memory_key": "p", "value": "after", "user_id": "u-cut"})
        got = await a.call(
            "memory.get", {"memory_key": "p", "user_id": "u-cut", "valid_as_of": jan(1)}
        )
        assert got["value"] == "after"


async def test_a_commit_that_landed_late_is_not_the_journal(
    control_plane_url, mint_test_token
) -> None:
    a, ns = await _agent(control_plane_url, mint_test_token, "late")
    async with a:
        await a.call("memory.set", {"memory_key": "k", "value": "on time"})
        ch = await _ch()
        try:
            row = await ch.query_one(
                "SELECT * EXCEPT inserted_at FROM loom.memory_temporal_commits "
                "WHERE org = {o:String} AND namespace = {n:String}",
                {"o": ORG, "n": ns},
            )
            row = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in row.items()}
            # An insert whose outcome was unknown, landing ten minutes after its stamp.
            row.update(
                commit_id=str(uuid.uuid4()),
                commit_seq=int(row["commit_seq"]) + 1,
                payload_sha256="e" * 64,
                recorded_at=datetime.now(UTC) - timedelta(minutes=10),
                retired_assertion_ids=[],
                assertions="[]",
                change_kind="retract",
            )
            await ch.insert("loom.memory_temporal_commits", [row])
        finally:
            await ch.disconnect()
        got = await a.call("memory.get", {"memory_key": "k", "valid_as_of": jan(1)})
        assert got["value"] == "on time" and got["temporal"]["complete"] is True


async def test_todays_fence_holds_when_both_projections_are_stale(
    control_plane_url, mint_test_token
) -> None:
    a, ns = await _agent(control_plane_url, mint_test_token, "stale-high")
    async with a:
        await a.call("memory.set", {"memory_key": "p", "value": "vault code tulip"})
        k = await _now_after()
        await a.call(
            "memory.set", {"memory_key": "p", "value": "vault code tulip", "sensitivity": "high"}
        )
        ch = await _ch()
        try:
            q = {"o": ORG, "n": ns}
            where = "WHERE org = {o:String} AND namespace = {n:String}"
            # Neither projection kept the 'high' revision, nor the watermark.
            await _delete(
                ch,
                f"DELETE FROM loom.memory_temporal_assertions {where} AND sensitivity = 'high'",
                **q,
            )
            await _delete(ch, f"DELETE FROM loom.memory {where} AND sensitivity = 'high'", **q)
            await _delete(
                ch,
                f"DELETE FROM loom.memory_temporal_watermarks {where} AND projection = 'assertions'",
                **q,
            )
        finally:
            await ch.disconnect()
        then = await a.call("memory.search", {"query": "vault code tulip", "known_as_of": k})
        assert all(r["memory_key"] != "p" for r in then["results"])


async def test_same_value_metadata_reaches_journal_diff_and_current_row(
    control_plane_url, mint_test_token
) -> None:
    from server.memory.app import temporal_projection as tp

    a, ns = await _agent(control_plane_url, mint_test_token, "meta")
    async with a:
        await a.call("memory.set", {"memory_key": "c", "value": "Berlin", "entities": ["berlin"]})
        await a.call(
            "memory.set",
            {
                "memory_key": "c",
                "value": "Berlin",
                "memory_type": "observation",
                "entities": ["munich"],
            },
        )
        got = await a.call("memory.get", {"memory_key": "c", "valid_as_of": jan(1)})
        assert got["memory_type"] == "observation" and got["entities"] == ["munich"]
        await a.call("memory.set", {"memory_key": "t", "value": "Lisbon", "tags": ["old"]})
        await a.call("memory.set", {"memory_key": "t", "value": "Lisbon", "tags": ["new"]})
        diff = await a.call("memory.temporal_diff", {"memory_key": "t", "mode": "belief"})
        assert diff["changes"][-1]["after_assertions"][0]["tags"] == ["new"]
        ch = await _ch()
        try:
            # The tag change's current row was never written: its key holds a stale
            # row, built here rather than left to merges keeping the older version.
            where = "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = 't'"
            await ch.execute(
                f"INSERT INTO loom.memory SELECT * REPLACE (['old'] AS tags) "
                f"FROM loom.memory FINAL {where}",
                params={"o": ORG, "n": ns},
            )
            await _delete(ch, f"DELETE FROM loom.memory {where} AND has(tags, 'new')", o=ORG, n=ns)
            assert (await a.call("memory.get", {"memory_key": "t"}))["tags"] == ["old"]
            for _ in range(50):
                await tp.reconcile_current(settle_s=0, only=(ORG, ns))
                if (await a.call("memory.get", {"memory_key": "t"}))["tags"] == ["new"]:
                    break
        finally:
            await ch.disconnect()
        assert (await a.call("memory.get", {"memory_key": "t"}))["tags"] == ["new"]


async def test_a_retry_writes_its_row_under_the_committed_id(
    control_plane_url, mint_test_token
) -> None:
    a, ns = await _agent(control_plane_url, mint_test_token, "retry-id")
    async with a:
        key = f"r-{uuid.uuid4().hex}"
        first = await a.call("memory.set", {"memory_key": "n", "value": "v"}, idempotency_key=key)
        ch = await _ch()
        try:
            # The row insert failed, and the replay cache has expired.
            await _delete(
                ch,
                "DELETE FROM loom.memory WHERE org = {o:String} AND namespace = {n:String}",
                o=ORG,
                n=ns,
            )
            await _delete(ch, "DELETE FROM loom.idempotency WHERE 1")
        finally:
            await ch.disconnect()
        again = await a.call("memory.set", {"memory_key": "n", "value": "v"}, idempotency_key=key)
        assert again["commit_seq"] == first["commit_seq"]
        assert again["memory_id"] == first["memory_id"]
        got = await a.call(
            "memory.get_by_id", {"memory_id": again["memory_id"], "valid_as_of": jan(1)}
        )
        assert got["found"] is True


async def test_a_durable_retry_is_answered_before_a_later_pin_refuses_it(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "retry-pin")
    async with a:
        key = f"r-{uuid.uuid4().hex}"
        first = await a.call("memory.set", {"memory_key": "p", "value": "A"}, idempotency_key=key)
        await a.call("memory.set", {"memory_key": "p", "value": "B", "pinned": True})
        ch = await _ch()
        try:
            await _delete(ch, "DELETE FROM loom.idempotency WHERE 1")  # the replay cache expired
        finally:
            await ch.disconnect()
        again = await a.call("memory.set", {"memory_key": "p", "value": "A"}, idempotency_key=key)
        assert again["commit_seq"] == first["commit_seq"]  # its receipt, not a 409
        assert (await a.call("memory.get", {"memory_key": "p"}))["value"] == "B"


async def test_retracting_the_current_claim_leaves_no_row_for_it_when_only_plans_remain(
    control_plane_url, mint_test_token
) -> None:
    a, _ = await _agent(control_plane_url, mint_test_token, "only-plans")
    later = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    async with a:
        await a.call("memory.set", {"memory_key": "r", "value": "A", "qualifiers": {"role": "a"}})
        await a.call(
            "memory.amend",
            {"memory_key": "r", "change_kind": "transition", "value": "B",
             "effective_at": later, "qualifiers": {"role": "b"}},
        )  # fmt: skip
        await a.call(
            "memory.amend",
            {"memory_key": "r", "change_kind": "retract", "qualifiers": {"role": "a"}},
        )
        assert (await a.call("memory.get", {"memory_key": "r"}))["found"] is False


async def test_clear_expiry_on_a_set_clears_it(control_plane_url, mint_test_token) -> None:
    # The caller stated the expiry: the journal's earlier deadline is not carried.
    a, _ = await _agent(control_plane_url, mint_test_token, "clearexp")
    async with a:
        await a.call("memory.set", {"memory_key": "e", "value": "v", "ttl_seconds": 3600})
        assert (await a.call("memory.get", {"memory_key": "e"}))["expires_at"]
        await a.call("memory.set", {"memory_key": "e", "value": "v", "clear_expiry": True})
        assert (await a.call("memory.get", {"memory_key": "e"}))["expires_at"] is None
