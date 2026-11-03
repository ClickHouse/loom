"""`LOOM_CODE_INTEL_ENABLED=false` refuses the `code.repo.*` ops too.

The switch is checked at the top of every `code.*` handler (`require_code_intel`).
The repository-admin handlers registered, re-pointed and retired repositories
with it off. CH-free: each handler must refuse before it reads anything.
"""

from __future__ import annotations

import pytest

from server.core.errors import PreconditionFailedError
from server.operations.registry import ALL_OPERATIONS

_REPO_OPS = sorted(op.name for op in ALL_OPERATIONS if op.name.startswith("code.repo."))


def test_the_repository_admin_ops_are_the_six_this_covers() -> None:
    assert _REPO_OPS == [
        "code.repo.add",
        "code.repo.add_branch",
        "code.repo.remote_branches",
        "code.repo.remove",
        "code.repo.set_access",
        "code.repo.set_source",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _REPO_OPS)
async def test_a_repository_admin_op_refuses_with_code_intelligence_off(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from server.code.app import common

    monkeypatch.setattr(common.settings, "code_intel_enabled", False)
    op = next(op for op in ALL_OPERATIONS if op.name == name)
    with pytest.raises(PreconditionFailedError, match="code intelligence is disabled"):
        await op.handler(op.input_model.model_construct())
