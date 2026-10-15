"""What a loom.memory version row means to the journal: a new claim, an assessment
of the served one, a retraction, or the transition / correction its writer declared."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from server.memory.domain import temporal_model as tm
from server.memory.domain.temporal_capture import (
    WriteIntent,
    amendment_for_row,
    intent_for,
)


def row(value: str = '"A"', **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": value,
        "content_sha256": value,
        "status": "active",
        "tombstoned_at": None,
        "redacted_at": None,
        "valid_at": None,
        "valid_to": None,
        "temporal_anchor": None,
        "version": 1,
    }
    base.update(kw)
    return base


def d(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def test_a_first_write_asserts_with_its_stated_validity() -> None:
    a = amendment_for_row(row(valid_at=datetime(2026, 1, 1)), [], WriteIntent())
    assert a.change_kind == "assert" and not a.retired
    (new,) = a.added
    assert new.valid_from == d(1) and new.validity_basis == "stated"


def test_an_undated_write_discloses_unknown_validity_not_the_ingest_clock() -> None:
    (new,) = amendment_for_row(row(), [], WriteIntent()).added
    assert new.valid_from is None and new.validity_basis == "unknown"
    (anchored,) = amendment_for_row(
        row(temporal_anchor=datetime(2026, 1, 3)), [], WriteIntent()
    ).added
    assert anchored.valid_from == d(3) and anchored.validity_basis == "extracted"


def test_a_restatement_assesses_the_current_segment_and_keeps_the_others() -> None:
    # After a correction the key serves three segments; a restatement of the head
    # row's value revises the segment that holds now; the others keep their own.
    a = tm.first('"A"', valid_from=d(1))
    served = list(
        tm.apply({a.assertion_id: a}, tm.correct([a], d(5), d(15), tm.first('"B"'))).values()
    )
    head = row('"A"', valid_at=datetime(2026, 1, 15))
    verify = row('"A"', valid_at=datetime(2026, 1, 15), verified_by="human:x")
    out = amendment_for_row(verify, served, WriteIntent(), head)
    assert out.change_kind == "assess"
    (b,) = out.added
    assert (b.value, b.valid_from, b.verified_by) == ('"A"', d(15), "human:x")
    after = tm.apply({a.assertion_id: a for a in served}, out).values()
    assert sorted(x.value for x in after) == ['"A"', '"A"', '"B"']


def test_a_counter_only_write_changes_nothing() -> None:
    (a,) = amendment_for_row(row(), [], WriteIntent()).added
    again = row(recall_count=9)
    assert amendment_for_row(again, [a], WriteIntent(), row()).empty


def test_a_new_value_replaces_what_the_key_served() -> None:
    (a,) = amendment_for_row(row(), [], WriteIntent()).added
    out = amendment_for_row(row('"B"'), [a], WriteIntent(), row())
    assert out.change_kind == "assert" and out.retired == (a.assertion_id,)
    assert out.added[0].origin_assertion_id == a.origin_assertion_id


def test_a_tombstone_or_redaction_retracts() -> None:
    (a,) = amendment_for_row(row(), [], WriteIntent()).added
    gone = amendment_for_row(row(tombstoned_at=datetime(2026, 2, 1)), [a], WriteIntent())
    assert gone.change_kind == "retract" and gone.retired == (a.assertion_id,)
    scrub = amendment_for_row(row(redacted_at=datetime(2026, 2, 1)), [a], WriteIntent())
    assert scrub.change_kind == "redact"
    superseded = amendment_for_row(row(status="superseded"), [a], WriteIntent())
    assert superseded.retired == (a.assertion_id,)


def test_a_declared_transition_applies_once() -> None:
    (a,) = amendment_for_row(row(valid_at=datetime(2026, 1, 1)), [], WriteIntent()).added
    intent = WriteIntent(memory_key="k", change_kind="transition", effective_at=d(10))
    first = amendment_for_row(row('"B"'), [a], intent, row())
    assert first.change_kind == "transition"
    served = list(tm.apply({a.assertion_id: a}, first).values())
    # A later row of the key in the same request (a status flip) is captured plainly.
    again = amendment_for_row(row('"B"', status="conflicted"), served, intent, row('"B"'))
    assert again.change_kind == "assess"


def test_an_intent_names_its_key() -> None:
    intent = WriteIntent(memory_key="k", change_kind="transition", effective_at=d(1))
    assert intent_for(row(), intent) is intent
    assert intent_for(row(memory_key="peer"), intent).change_kind == ""


def test_an_attestation_lands_only_on_the_content_it_hashes() -> None:
    # A -> B over a span, A now: verifying the head binds A's hash, not B's.
    a = tm.first('"A"', valid_from=d(1))
    served = list(
        tm.apply({a.assertion_id: a}, tm.correct([a], d(5), d(15), tm.first('"B"'))).values()
    )
    head = row('"A"', valid_at=datetime(2026, 1, 15))
    verify = row(
        '"A"',
        valid_at=datetime(2026, 1, 15),
        verified_by="human:x",
        verified_sha256=a.content_sha256,
    )
    out = amendment_for_row(verify, served, WriteIntent(), head)
    by_value = {x.value: x.verified_by for x in out.added}
    assert by_value == {'"A"': "human:x"}
    assert all(x.value == '"A"' for x in out.added)


def test_a_declared_shape_on_unchanged_content_is_a_revision() -> None:
    (planned,) = amendment_for_row(
        row('"Lisbon"'), [], WriteIntent(memory_key="k", modality="planned")
    ).added
    out = amendment_for_row(
        row('"Lisbon"'),
        [planned],
        WriteIntent(memory_key="k", modality="asserted"),
        row('"Lisbon"'),
    )
    assert out.change_kind == "assert" and out.retired == (planned.assertion_id,)
    assert out.added[0].modality == "asserted"


def test_a_procedural_hash_is_of_the_text_memory_set_embeds() -> None:
    import hashlib
    import json

    from server.memory.domain.embed_text import embedding_texts
    from server.memory.domain.temporal_capture import embed_input_sha256

    proc = {"situation": "deploy fails", "name": "roll_back", "steps": ["revert"], "uses": 7}
    stored = json.dumps(proc)
    _, live, _ = embedding_texts("procedural", proc, stored, "proc/deploy")
    assert "uses" not in live  # the counters never enter the vector
    assert embed_input_sha256("procedural", stored, "proc/deploy") == (
        hashlib.sha256(live.encode()).hexdigest()
    )


def test_a_later_date_for_a_value_already_held_is_a_restatement() -> None:
    from server.memory.domain.temporal_capture import (
        WriteIntent,
        amendment_for_row,
        assertion_from_row,
    )

    base = {
        "memory_key": "k", "value": "Alice lives in Paris", "memory_type": "semantic",
        "status": "active", "content_sha256": "abc", "valid_at": None, "valid_to": None,
        "temporal_anchor": datetime(2024, 1, 1),
    }  # fmt: skip
    first = assertion_from_row(base, WriteIntent())
    later = {**base, "temporal_anchor": datetime(2025, 6, 1)}
    # A held from 2024 already covers "from 2025": a plain write narrows nothing
    # (memory.amend corrects a start).
    assert amendment_for_row(later, [first], WriteIntent(), head=base).empty


def test_a_retyped_assessment_rehashes_its_embed_text() -> None:
    import json

    from server.memory.domain.temporal_capture import (
        WriteIntent,
        amendment_for_row,
        assertion_from_row,
        embed_input_sha256,
    )

    proc = json.dumps({"situation": "deploy fails", "name": "roll_back", "steps": ["revert"]})
    head = {
        "memory_key": "p", "value": proc, "memory_type": "semantic", "status": "active",
        "content_sha256": "h", "valid_at": None, "valid_to": None, "temporal_anchor": None,
    }  # fmt: skip
    served = [assertion_from_row(head, WriteIntent())]
    am = amendment_for_row({**head, "memory_type": "procedural"}, served, WriteIntent(), head=head)
    assert am.change_kind == "assess"
    assert [a.embed_input_sha256 for a in am.added] == [embed_input_sha256("procedural", proc, "p")]


def test_a_qualified_write_takes_its_shape_from_its_own_qualifiers() -> None:
    snap = tm.first('"100"', temporal_kind="snapshot", valid_from=d(1))
    intent = WriteIntent(memory_key="k", qualifiers={"acct": "x"})
    first = amendment_for_row(row('"200"'), [snap], intent)
    (b,) = first.added
    assert b.temporal_kind == "state"
    served = list(tm.apply({snap.assertion_id: snap}, first).values())
    assert amendment_for_row(row('"200"'), served, intent).empty


def test_a_declared_kind_inherits_the_keys_qualifiers_and_is_idempotent() -> None:
    state = tm.first('"100"', qualifiers={"acct": "x"}, valid_from=d(1))
    intent = WriteIntent(memory_key="k", temporal_kind="event")
    first = amendment_for_row(row('"200"', valid_at=datetime(2026, 1, 5)), [state], intent)
    (e,) = first.added
    assert (e.temporal_kind, e.qualifiers) == ("event", {"acct": "x"})
    served = list(tm.apply({state.assertion_id: state}, first).values())
    assert amendment_for_row(row('"200"', valid_at=datetime(2026, 1, 5)), served, intent).empty


def test_a_metadata_write_leaves_another_qualifiers_claim_alone() -> None:
    plain = tm.first('"qv"', valid_from=d(1))
    eu = tm.first('"qv"', valid_from=d(1), qualifiers={"r": "eu"}, tags=("keep",))
    out = amendment_for_row(
        row('"qv"', valid_at=datetime(2026, 1, 1), tags=["x"]),
        [plain, eu],
        WriteIntent(),
        row('"qv"', valid_at=datetime(2026, 1, 1)),
    )
    assert out.retired == (plain.assertion_id,)


def test_a_restatement_leaves_the_same_texts_past_and_plans_alone() -> None:
    now = datetime.now(UTC)
    past = tm.first(
        '"t"',
        valid_from=now - timedelta(days=300),
        valid_to=now - timedelta(days=200),
        sensitivity="high",
    )
    cur = tm.first('"t"', valid_from=now - timedelta(days=10))
    plan = tm.first('"t"', valid_from=now + timedelta(days=30), pinned=True)
    cur = replace(cur, valid_to=plan.valid_from)
    head = row(
        '"t"',
        valid_at=cur.valid_from.replace(tzinfo=None),
        valid_to=cur.valid_to.replace(tzinfo=None),
    )
    again = row(
        '"t"',
        valid_at=cur.valid_from.replace(tzinfo=None),
        valid_to=cur.valid_to.replace(tzinfo=None),
        tags=["new"],
    )
    out = amendment_for_row(again, [past, cur, plan], WriteIntent(), head, now=now)
    assert out.retired == (cur.assertion_id,)
