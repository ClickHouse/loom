"""Fresh store proof with bounded retries and prepare's store-specific nonce cleanup."""

from __future__ import annotations

import asyncio
from typing import Any

from server.cli import cell_control, cell_prepare
from server.router.app import REGISTER_ANSWER_MAX_S

# Read/probe/commit bounds already used by prepare, plus its reply margin.
ANSWER_S = REGISTER_ANSWER_MAX_S + 10.0


async def post(run: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    await cell_control.check_lock(run.owner.conn)
    nonces = cell_prepare._new_nonces()

    async def clean() -> None:
        await cell_prepare._delete_nonces(run.ch, nonces)

    try:
        await run.owner.call(
            lambda: cell_prepare._write_nonces(run.ch, nonces), total_s=cell_control.TRANSACTION_S
        )
        async with asyncio.timeout(run.args.wait + ANSWER_S):
            answer = await cell_prepare._register(
                run.args,
                run.router,
                {**body, "nonces": nonces, **cell_control.preconditions(run.row)},
                run.transport,
                path=path,
                command="update",
            )
        await cell_control.check_lock(run.owner.conn)
        cell_control.preconditions(answer)
    except BaseException:
        try:
            await run.owner.call(clean, total_s=cell_control.TRANSACTION_S)
        except BaseException:  # noqa: BLE001 — preserve the original operation failure.
            pass
        raise
    await run.owner.call(clean, total_s=cell_control.TRANSACTION_S)
    return answer
