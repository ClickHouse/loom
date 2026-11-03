"""`LOOM_CODE_INTEL_ENABLED=false` refuses every `code.*` op but the retirement.

The switch is checked at the top of every `code.*` handler (`require_code_intel`),
or of the function it hands off to. `code.repo.remove` only deletes, so it is
served with the switch off. CH-free: each refusing handler must refuse before it
reads its input or the store.
"""

from __future__ import annotations

import pytest

from server.core.errors import PreconditionFailedError
from server.operations.base import Operation
from server.operations.registry import ALL_OPERATIONS
from server.settings import settings

_SERVED_WITH_THE_SWITCH_OFF = {"code.repo.remove"}
_CODE_OPS = sorted(
    (
        op
        for op in ALL_OPERATIONS
        if op.name.startswith("code.") and op.name not in _SERVED_WITH_THE_SWITCH_OFF
    ),
    key=lambda op: op.name,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", _CODE_OPS, ids=lambda op: op.name)
async def test_a_code_op_refuses_with_code_intelligence_off(
    monkeypatch: pytest.MonkeyPatch, op: Operation
) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", False)
    with pytest.raises(PreconditionFailedError, match="code intelligence is disabled"):
        await op.handler(op.input_model.model_construct())


@pytest.mark.asyncio
async def test_a_retirement_is_not_behind_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the switch, an empty input fails on its first field, not on the switch."""
    monkeypatch.setattr(settings, "code_intel_enabled", False)
    op = next(op for op in ALL_OPERATIONS if op.name == "code.repo.remove")
    with pytest.raises(AttributeError):
        await op.handler(op.input_model.model_construct())
