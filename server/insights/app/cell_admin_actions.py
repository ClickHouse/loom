"""Bounded cell adapters for the ordinary settings, rate and budget owners."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel

from server.core import settings_overrides
from server.core.ch import statement_timeout
from server.core.ch_bounded import BoundedReadError
from server.insights.api.models_cell_admin import (
    CellBudgetSetInput,
    CellRatelimitNamespaceSetInput,
    CellSettingsHistoryInput,
    CellSettingsListInput,
    CellSettingsSetInput,
)
from server.insights.app import limit_changes, settings_changes
from server.insights.app.admin_models import (
    BudgetSetOutput,
    RatelimitSetOutput,
    SettingChange,
    SettingRow,
    SettingSetOutput,
    SettingsHistoryOutput,
    SettingsListOutput,
)


@asynccontextmanager
async def _allowance(deadline: float) -> AsyncIterator[float]:
    # The caller's earlier auth/audit deadline remains authoritative.
    deadline = min(deadline, time.monotonic() + 30.0)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BoundedReadError("Cell action allowance exhausted")
    async with asyncio.timeout_at(deadline):
        with statement_timeout(min(2.0, remaining)):
            yield deadline


def _result(model: BaseModel) -> dict[str, Any]:
    result = model.model_dump(mode="json")
    encoded = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(encoded.encode("utf-8")) > 2 * 1024**2:
        raise BoundedReadError("Cell action result exceeds its allowance")
    return result


async def settings_list(
    input: CellSettingsListInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    async with _allowance(deadline) as deadline:
        await settings_overrides.refresh_bounded(deadline=deadline, ch=ch)
        rows = settings_overrides.selected_rows(prefix=input.prefix)
        return _result(SettingsListOutput(settings=[SettingRow(**row) for row in rows]))


async def settings_set(
    input: CellSettingsSetInput,
    *,
    actor: str,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    async with _allowance(deadline) as deadline:
        row = await settings_changes.change(
            input.name,
            input.value,
            clear=input.clear,
            expected=(
                input.expected if input.expected is not None else settings_overrides.NOT_GIVEN
            ),
            actor=actor,
            ch=ch,
            deadline=deadline,
        )
        return _result(SettingSetOutput(ok=True, setting=SettingRow(**row) if row else None))


async def settings_history(
    input: CellSettingsHistoryInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    async with _allowance(deadline) as deadline:
        rows = await settings_changes.history(input.name, input.limit, ch=ch, deadline=deadline)
        return _result(SettingsHistoryOutput(changes=[SettingChange(**row) for row in rows]))


async def ratelimit_namespace_set(
    input: CellRatelimitNamespaceSetInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    async with _allowance(deadline) as deadline:
        result = await limit_changes.change_namespace_rate(
            input.namespace,
            input.op_class,
            capacity=input.capacity,
            rate=input.rate,
            clear=input.clear,
            deadline=deadline,
            ch=ch,
        )
        return _result(RatelimitSetOutput.model_validate(result))


async def budget_set(
    input: CellBudgetSetInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    from server.operations.registry import OPERATIONS_BY_NAME

    if len(OPERATIONS_BY_NAME) > 4096 or input.op != "*" and input.op not in OPERATIONS_BY_NAME:
        raise BoundedReadError("Unsupported operation budget")
    async with _allowance(deadline) as deadline:
        result = await limit_changes.change_budget(
            input.op,
            consumer=input.consumer,
            budget_ms=input.budget_ms,
            budget=input.budget,
            clear=input.clear,
            deadline=deadline,
            ch=ch,
        )
        return _result(BudgetSetOutput.model_validate(result))
