"""`LOOM_CODE_INTEL_ENABLED=false` refuses every `code.*` op.

The switch is checked at the top of every `code.*` handler (`require_code_intel`),
or of the function it hands off to. The repository-admin handlers (`code.repo.*`)
registered, re-pointed and retired repositories with it off. CH-free: each
handler must refuse before it reads its input or the store.
"""

from __future__ import annotations

import pytest

from server.core.errors import PreconditionFailedError
from server.operations.registry import ALL_OPERATIONS

_CODE_OPS = sorted(op.name for op in ALL_OPERATIONS if op.name.startswith("code."))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _CODE_OPS)
async def test_a_code_op_refuses_with_code_intelligence_off(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from server.code.app import common

    monkeypatch.setattr(common.settings, "code_intel_enabled", False)
    op = next(op for op in ALL_OPERATIONS if op.name == name)
    with pytest.raises(PreconditionFailedError, match="code intelligence is disabled"):
        await op.handler(op.input_model.model_construct())
