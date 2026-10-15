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

    async def _served(org: str, cut: reads.Cut, **_: Any) -> list[reads.Held]:
        side = then if cut.axes.known is not None else now
        return [reads.Held(a, "a", "-", "-", k, "m") for k, al in side.items() for a in al]

    monkeypatch.setattr(reads, "cutoff", _cutoff)
    monkeypatch.setattr(reads, "served", _served)
    plan = await rb.plan("o", "n", datetime(2026, 2, 1, tzinfo=UTC), memory_keys=None, limit=10)
    by_key = {c.ident[3]: c for c in plan.changes}
    assert sorted(by_key) == ["born", "secret"]
    assert by_key["born"].action == "remove" and by_key["secret"].then == ()
