"""memory.amend: the assertion it builds and the change it plans."""

from __future__ import annotations

from types import SimpleNamespace

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


def test_a_high_key_stays_high_and_a_secret_value_reads_high() -> None:
    assert _new_assertion(_amend("hello"), _head("high")).sensitivity == "high"
    assert _new_assertion(_amend("password: hunter2xyz"), _head("")).sensitivity == "high"
    assert _new_assertion(_amend("hello"), _head("")).sensitivity == ""


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
