"""memory.amend: the assertion it builds and the change it plans."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from server.memory.api.models_temporal import MemoryAmendInput
from server.memory.app.temporal_ops import _new_assertion


def _amend(value: str) -> MemoryAmendInput:
    return MemoryAmendInput(
        org="o", namespace="n", agent="a", memory_key="k", change_kind="correct", value=value
    )


def _head(sensitivity: str) -> SimpleNamespace:
    return SimpleNamespace(
        memory_type="semantic", sensitivity=sensitivity, expires_at=None, pinned=False,
        lock_mode="", tags=[], entities=[],
    )  # fmt: skip


def _built(value: str, served: list) -> Any:
    from server.memory.app.temporal_ops import _amendment_builder

    inp = _amend(value)
    am = _amendment_builder(inp, _new_assertion(inp, _head("")))(served)
    (added,) = am.added
    return added


def test_a_high_key_stays_high_and_a_secret_value_reads_high() -> None:
    from server.memory.domain import temporal_model as tm

    assert _built('"hello"', [tm.first('"x"', sensitivity="high")]).sensitivity == "high"
    assert _built('"password: hunter2xyz"', [tm.first('"x"')]).sensitivity == "high"
    assert _built('"hello"', [tm.first('"x"')]).sensitivity == ""


def test_an_amendment_takes_the_metadata_the_journal_holds_when_it_is_planned() -> None:
    """Not the head the handler read: a pin, tags and a timed lock committed since
    are carried, the whole lock (its deadline and reason with its mode)."""
    from datetime import UTC, datetime, timedelta

    from server.memory.domain import temporal_model as tm

    until = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    held = tm.first(
        '"x"', pinned=True, tags=("t",), lock_mode="no_delete", lock_until=until,
        lock_reason="audit", locked_by="human:ann",
    )  # fmt: skip
    a = _built('"y"', [held])
    assert (a.pinned, a.tags, a.lock_mode, a.lock_until, a.lock_reason, a.locked_by) == (
        True, ("t",), "no_delete", until, "audit", "human:ann",
    )  # fmt: skip


def test_a_lock_taken_since_the_handler_read_refuses_the_amendment() -> None:
    import pytest
    from fastapi import HTTPException

    from server.memory.domain import temporal_model as tm

    with pytest.raises(HTTPException) as e:
        _built('"y"', [tm.first('"x"', lock_mode="read_only")])
    assert e.value.status_code == 423


def test_an_amendment_over_the_keys_char_limit_is_refused(monkeypatch) -> None:
    import asyncio

    import pytest
    from fastapi import HTTPException

    import server.memory.store as memory_repo
    from server.memory.app import blacklist_guard
    from server.memory.app.temporal_ops import _amend_guards

    async def _get(*_a, **_k):
        return SimpleNamespace(**vars(_head("")), char_limit=4, lock_until=None)

    async def _clean(**_k):
        return None

    monkeypatch.setattr(memory_repo, "get_by_key", _get)
    monkeypatch.setattr(blacklist_guard, "enforce_on_insert", _clean)
    with pytest.raises(HTTPException) as e:
        asyncio.run(_amend_guards(_amend("forty characters of text, well over four")))
    assert e.value.detail["code"] == "CHAR_LIMIT_EXCEEDED"


def test_a_retract_under_qualifiers_keeps_the_other_qualifiers_claim() -> None:
    from server.memory.app.temporal_ops import _amendment_builder
    from server.memory.domain import temporal_model as tm

    ceo = tm.first('"Ann"', qualifiers={"role": "ceo"})
    cto = tm.first('"Bob"', qualifiers={"role": "cto"})
    retract = MemoryAmendInput(
        org="o", namespace="n", agent="a", memory_key="k", change_kind="retract",
        qualifiers={"role": "ceo"},
    )  # fmt: skip
    am = _amendment_builder(retract, None)([ceo, cto])
    assert am.retired == (ceo.assertion_id,)


def test_historical_reads_refuse_options_only_today_can_answer() -> None:
    import asyncio
    from datetime import UTC, datetime

    import pytest

    from server.core.errors import ValidationError
    from server.memory.api.models_crud import MemoryGetInput, MemoryListInput
    from server.memory.app import temporal_ops

    k = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="follow"):
        asyncio.run(
            temporal_ops.get(
                MemoryGetInput(
                    org="o", namespace="n", agent="a", memory_key="k", known_as_of=k, follow=True
                )
            )
        )
    with pytest.raises(ValidationError, match="include_superseded"):
        asyncio.run(
            temporal_ops.list_at(
                MemoryListInput(
                    org="o", namespace="n", agent="a", known_as_of=k, include_superseded=True
                )
            )
        )


def test_an_amended_value_takes_its_tiers_policy(monkeypatch) -> None:
    """A value that reads higher than the key's tier raises it, and that tier's policy
    applies as on memory.set: the retention window and the one level label."""
    from datetime import UTC, datetime, timedelta

    from server.memory.domain import temporal_model as tm
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitive_retention_days", 1)
    note = tm.first('"a note"', tags=("work",))
    a = _built('"reach me at alice@example.com"', [note])
    assert a.sensitivity == "medium" and "sensitivity:medium" in a.tags and "work" in a.tags
    assert a.expires_at is not None and a.expires_at <= datetime.now(UTC) + timedelta(days=1)
    medium = tm.first('"alice@example.com"', sensitivity="medium", tags=("sensitivity:medium",))
    b = _built('"password: hunter2xyz"', [medium])
    assert b.sensitivity == "high" and b.tags == ("sensitivity:high",)


def test_a_claim_under_new_qualifiers_inherits_nothing_of_another() -> None:
    from server.memory.app.temporal_ops import _amendment_builder
    from server.memory.domain import temporal_model as tm

    eu = tm.first('"x"', qualifiers={"region": "eu"}, pinned=True, tags=("eu",))
    inp = MemoryAmendInput(
        org="o", namespace="n", agent="a", memory_key="k", change_kind="correct", value="y",
        qualifiers={"region": "us"},
    )  # fmt: skip
    (us,) = _amendment_builder(inp, _new_assertion(inp, _head("")))([eu]).added
    assert (us.pinned, us.tags) == (False, ())
