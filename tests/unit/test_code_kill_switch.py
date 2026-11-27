"""`LOOM_CODE_INTEL_ENABLED=false` refuses every `code.*` op but the retirement.

The switch is checked at the top of every `code.*` handler (`require_code_intel`),
or of the function it hands off to. `code.repo.remove` only deletes, so it is
served with the switch off. CH-free: each refusing handler must refuse before it
reads its input or the store.
"""

from __future__ import annotations

from contextlib import nullcontext

import pytest

from server.code.api.models_repos import CodeRepoRemoveInput
from server.code.app.repo_admin import handle_code_repo_remove
from server.core.errors import PreconditionFailedError, ValidationError
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
    """With code intelligence off, retirement still looks up the registry entry."""
    from server.code.app import repo_admin
    from server.code.store.index import CodeRepo
    from server.core import ch
    from server.knowledge.app import sources

    async def missing(*_args):
        return None

    monkeypatch.setattr(ch, "get_client", object)
    monkeypatch.setattr(repo_admin, "account_change", lambda *_: nullcontext())
    monkeypatch.setattr(sources, "sources_lease", lambda *_: nullcontext())
    monkeypatch.setattr(CodeRepo, "code_repo_row", missing)
    monkeypatch.setattr(settings, "code_intel_enabled", False)
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    with pytest.raises(ValidationError) as refused:
        await handle_code_repo_remove(
            CodeRepoRemoveInput(org="o", namespace="n", confirm="n", confirm_namespace_purge=True)
        )
    assert refused.value.to_detail()["code"] == "NOT_A_REPO"
