"""namespace.rollback in the journal: a changed key serves again exactly what it served
at `as_of` (spans, plans and kinds as they were), and a lock refuses the change."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.memory.app import namespace_rollback as rb
from server.memory.app import temporal_reads as reads
from server.memory.domain import temporal_model as tm
from server.memory.store.redaction import REDACTED_VALUE

_IDENT = ("a", "-", "-", "office")


def _change(then: list[tm.Assertion], now: list[tm.Assertion]) -> rb.KeyChange:
    return rb.KeyChange(ident=_IDENT, memory_id="m", then=tuple(then), now=tuple(now))


def test_the_then_claims_come_back_as_they_were() -> None:
    jan, later = datetime(2026, 1, 1, tzinfo=UTC), datetime.now(UTC) + timedelta(days=30)
    span = tm.first('"Leeds"', valid_from=jan, valid_to=datetime(2030, 1, 1, tzinfo=UTC))
    plan = tm.first('"York"', valid_from=later)
    ten = tm.first('"10"', temporal_kind="snapshot", valid_from=jan)
    now = tm.first('"Paris"')
    am = rb._build(_change([span, plan, ten], [now]))([now])
    assert am.retired == (now.assertion_id,)
    strip = lambda a: {k: v for k, v in a.to_json().items() if k not in rb._IDS}  # noqa: E731
    assert [strip(a) for a in am.added] == [strip(span), strip(plan), strip(ten)]
    assert all(a.parent_assertion_id in {span.assertion_id, plan.assertion_id, ten.assertion_id}
               for a in am.added)  # fmt: skip


def test_a_lock_in_force_leaves_the_key_as_it_is() -> None:
    held = tm.first('"Paris"', lock_mode="read_only")
    assert rb._build(_change([tm.first('"Leeds"')], [held]))([held]).empty
    assert rb._build(_change([], [tm.first('"x"', lock_mode="no_delete")]))(
        [tm.first('"x"', lock_mode="no_delete")]
    ).empty


@pytest.mark.asyncio
async def test_the_plan_names_only_changed_keys_and_never_a_redacted_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same, gone = tm.first('"same"'), tm.first(REDACTED_VALUE)
    then = {"same": [same], "secret": [gone], "born": []}
    now = {"same": [same], "secret": [tm.first('"new"')], "born": [tm.first('"b"')]}

    async def _cutoff(org: str, ns: str, axes: Any, **_: Any) -> reads.Cut:
        cut = reads.Cut(namespace=ns, axes=axes)
        cut.first_at, cut.seq, cut.watermark = datetime(2026, 1, 1, tzinfo=UTC), 5, 5
        return cut

    async def _served_at(org: str, cut: reads.Cut, **_: Any) -> dict[str, reads.Held]:
        side = then if cut.axes.known is not None else now
        return {
            a.assertion_id: reads.Held(a, "a", "-", "-", k, "m")
            for k, al in side.items()
            for a in al
        }

    async def _visible(org: str, ns: str, held: list[reads.Held]) -> list[reads.Held]:
        return held

    async def _keys(*_a: Any, **_k: Any) -> list[str]:
        return ["born", "same", "secret"]

    from server.memory.store import temporal as journal

    monkeypatch.setattr(reads, "cutoff", _cutoff)
    monkeypatch.setattr(reads, "_served_at", _served_at)
    monkeypatch.setattr(reads, "visible", _visible)

    async def _seeded(*_a: Any, **_k: Any) -> set[Any]:
        return set()

    monkeypatch.setattr(journal, "journal_keys", _keys)
    monkeypatch.setattr(journal, "seeded_after", _seeded)
    plan = await rb.plan("o", "n", datetime(2026, 2, 1, tzinfo=UTC), memory_keys=None, limit=10)
    by_key = {c.ident[3]: c for c in plan.changes}
    assert sorted(by_key) == ["born", "secret"]
    # A key that served a value since redacted is left as it is, never emptied.
    assert by_key["born"].action == "remove" and not by_key["born"].refused
    assert by_key["secret"].refused
    assert by_key["secret"].sample()["then"] == ["[withheld]"]


def test_a_partial_read_never_plans_a_rollback() -> None:
    """A truncated page would plan removals of keys it did not see; any other gap
    (an allocation in doubt, an integrity break) reads a partial journal."""
    from server.core.errors import TemporalUnavailableError, ValidationError

    def _cut(*gaps: str) -> reads.Cut:
        cut = reads.Cut(namespace="n", axes=None)  # type: ignore[arg-type]
        cut.gaps = list(gaps)
        return cut

    rb._refuse_gaps(_cut(), _cut())
    with pytest.raises(ValidationError, match="memory_keys"):
        rb._refuse_gaps(_cut(), _cut("truncated:10000"))
    with pytest.raises(TemporalUnavailableError, match="allocation_in_doubt"):
        rb._refuse_gaps(_cut("allocation_in_doubt:7"), _cut())


def test_the_lock_in_force_stays_and_one_lifted_since_is_not_taken_again() -> None:
    then = tm.first('"Leeds"', lock_mode="read_only", locked_by="human:ann")
    held = tm.first('"Paris"', lock_mode="no_delete", lock_reason="audit")
    (back,) = rb._build(_change([then], [held]))([held]).added
    assert (back.lock_mode, back.lock_reason, back.locked_by) == ("no_delete", "audit", "")
    (free,) = rb._build(_change([then], [tm.first('"Paris"')]))([tm.first('"Paris"')]).added
    assert free.lock_mode == ""


def test_rows_older_than_the_journal_do_not_block_a_rollback() -> None:
    cut = reads.Cut(namespace="n", axes=None)  # type: ignore[arg-type]
    cut.gaps = ["history_before_journal"]
    rb._refuse_gaps(cut, cut)  # the keys it concerns are the seeded ones, left out
