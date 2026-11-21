"""Fenced cell control: one directory claim and the exact local lock-owning backend."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from server.cli import cell_attach_pg as apg
from server.core import pg, retirement
from server.core.cell_ch import TRANSACTION_S, Refusal
from server.core.cell_ch import Refused as Refused
from server.router.directory import CONTROL_FIELDS, CONTROL_MAX_S
from server.settings import settings

OPEN_S = 75.0  # not a setting: connection initialization and direct-session checks
REQUEST_S = CONTROL_MAX_S + 30.0  # not a setting: directory bound plus probes/connect/close
Call = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _refused(reason: str) -> Refused:
    return Refused([Refusal("cell control", reason)])


def preconditions(row: dict[str, Any], *, claim: bool = False) -> dict[str, Any]:
    """Carry the whole observed registration and exact fingerprint pair."""
    if any(name not in row for name in (*CONTROL_FIELDS, "mutation_generation")):
        raise _refused("a router replica is not upgraded for cell control fencing")
    try:
        generation = str(UUID(row["mutation_generation"]))
    except (ValueError, TypeError, AttributeError):
        raise _refused(
            "a router replica returned no readable cell control generation: upgrade it"
        ) from None
    values = {name: row[name] for name in CONTROL_FIELDS}
    if not claim:
        values["mutation_generation"] = generation
    return {"expected": values}


async def check_lock(conn: Any) -> None:
    try:
        held = await apg.lock_held(conn)
    except Exception as e:  # noqa: BLE001 — an unreadable lock ends this ownership
        raise _refused(
            f"lock session lost ({type(e).__name__}): stop and rerun with a fresh claim"
        ) from None
    if not held:
        raise _refused("attach lock lost: stop and rerun with a fresh claim")


async def paired(conn: Any, row: dict[str, Any]) -> None:
    """The connected authority and configured bucket name the observed registration."""
    recorded = await conn.fetchval("SELECT deployment_id FROM loom.authority")
    prefix = settings.document_s3_prefix if settings.document_s3_bucket else ""
    if (
        not recorded
        or recorded != row.get("deployment_id")
        or settings.cell != row.get("cell")
        or (settings.document_s3_bucket, prefix) != (row.get("bucket"), row.get("bucket_prefix"))
    ):
        raise _refused(
            "this cell configuration and connected Postgres do not match the directory registration"
        )


async def claim(conn: Any, call: Call, row: dict[str, Any]) -> dict[str, Any]:
    """Claim once; a lost acknowledgement ends the attempt, never restoring an old token."""
    await check_lock(conn)
    await paired(conn, row)
    answer = await call("/claim", preconditions(row, claim=True))
    await check_lock(conn)
    preconditions(answer)
    expected = {name: row[name] for name in CONTROL_FIELDS}
    if json.dumps(expected, sort_keys=True) != json.dumps(
        {name: answer[name] for name in CONTROL_FIELDS}, sort_keys=True
    ):
        raise _refused(
            "claim returned a different registration or fingerprint pair: stop and rerun"
        )
    return answer


async def mutate(
    conn: Any, call: Call, row: dict[str, Any], path: str, body: dict[str, Any]
) -> dict[str, Any]:
    """An unknown response stops the caller; a fresh run must claim and recompute."""
    await check_lock(conn)
    answer = await call(path, {**body, **preconditions(row)})
    await check_lock(conn)
    return answer


@dataclass(frozen=True)
class _Fatal:
    error: SystemExit | KeyboardInterrupt


class _ForwardFatal(BaseException):
    def __init__(self, fatal: _Fatal) -> None:
        self.fatal = fatal


async def _captured[T](work: Callable[[], Awaitable[T]]) -> T | _Fatal:
    try:
        return await work()
    except (SystemExit, KeyboardInterrupt) as error:
        return _Fatal(error)
    except _ForwardFatal as error:
        return error.fatal


class Owner:
    """One raw backend and one cleanup owner for a native control command."""

    def __init__(self, *, exit_s: float = pg._RELEASE_TIMEOUT_S) -> None:
        self.exit_s = exit_s
        self.conn: Any = None
        self.phase_end: float | None = None
        self.close_end: float | None = None
        self.closed = False
        self._close_task: asyncio.Task[Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    def _start[T](self, work: Callable[[], Awaitable[T]]) -> asyncio.Task[T | _Fatal]:
        gate = retirement.get_gate()

        async def admitted() -> T | _Fatal:
            async with gate.admit():
                return await _captured(work)

        task = asyncio.create_task(admitted())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(pg._retrieve_task)
        return task

    def _bind(self, conn: Any) -> bool:
        self.conn = conn
        return not self.closed

    async def connect(self, *, statement_timeout_ms: int) -> None:
        if self.conn is not None or self.closed:
            raise _refused("a control attempt cannot open another session")
        try:
            await pg._connect_dedicated(statement_timeout_ms=statement_timeout_ms, bind=self._bind)
        except BaseException:
            with contextlib.suppress(BaseException):
                await self.close()
            raise

    async def _open(self, what: str) -> None:
        await self.connect(statement_timeout_ms=60_000)
        refused = await apg.direct_refusals(self.conn, what)
        refused += await apg.encoding_refusals(self.conn)
        if refused:
            raise Refused(refused)
        await apg.take_lock(self.conn, what)

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.close_end = self.phase_end or (time.monotonic() + self.exit_s)
        if self.conn is None:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                _captured(lambda: pg.close_owned(self.conn, end=cast(float, self.close_end)))
            )
            self._close_task.add_done_callback(pg._retrieve_task)
        interrupted: asyncio.CancelledError | None = None
        try:
            while not self._close_task.done():
                remaining = cast(float, self.close_end) - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("cell control cleanup deadline expired; outcome unknown")
                try:
                    await asyncio.wait({self._close_task}, timeout=remaining)
                except asyncio.CancelledError as error:
                    # Signals cannot abort the public cancellation already in flight.
                    interrupted = interrupted or error
            result = self._close_task.result()
            if isinstance(result, _Fatal):
                raise _ForwardFatal(result)
        except BaseException:
            try:
                pg._terminate(self.conn)
            except BaseException:  # noqa: BLE001 — abort cannot replace the first close failure.
                pass
            finally:
                if not self._close_task.done():
                    self._close_task.cancel()
            if interrupted is not None:
                raise interrupted from None
            raise
        if interrupted is not None:
            raise interrupted

    async def _stop(self, tasks: set[asyncio.Task[Any]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            if tasks:
                await asyncio.wait(tasks, timeout=max(0, (self.close_end or 0) - time.monotonic()))
        except BaseException:  # noqa: BLE001 — cleanup cannot replace the original failure.
            if self.conn is not None:
                try:
                    pg._terminate(self.conn)
                except BaseException:  # noqa: BLE001 — failed abort preserves the caller's failure.
                    pass

    async def call[T](self, work: Callable[[], Awaitable[T]], *, total_s: float) -> T:
        if self.closed or self.phase_end is not None:
            raise _refused("a closed or active control phase cannot start another phase")
        self.phase_end = time.monotonic() + total_s
        task = self._start(work)
        try:
            done, _ = await asyncio.wait(
                {task},
                timeout=max(0, self.phase_end - min(1.0, total_s / 4) - time.monotonic()),
            )
            if not done:
                raise TimeoutError(
                    "cell control phase deadline expired; outcome unknown; stop and rerun"
                )
            result = task.result()
            if isinstance(result, _Fatal):
                raise _ForwardFatal(result)
        except BaseException:
            try:
                await self.close()
            except BaseException:  # noqa: BLE001 — keep the original phase failure/cancellation.
                pass
            await self._stop({task})
            raise
        self.phase_end = None
        return result

    async def transaction[T](self, work: Callable[[], Awaitable[T]]) -> T:
        return await self.call(lambda: transaction(self.conn, work), total_s=TRANSACTION_S)

    async def run[T](
        self,
        opening: Callable[[], Awaitable[None]],
        work: Callable[[], Awaitable[T]],
        *,
        opening_s: float,
    ) -> T:
        """Own one command; fatal guard failures leave only after bounded cleanup."""
        if self.conn is not None or self.phase_end is not None or self._tasks or self.closed:
            raise _refused("a command needs a fresh control owner")
        try:
            await self.call(opening, total_s=opening_s)
            result = await asyncio.shield(self._start(work))
            if isinstance(result, _Fatal):
                raise _ForwardFatal(result)
        except BaseException as original:
            try:
                await self.close()
            except BaseException:  # noqa: BLE001 — preserve the command's original failure.
                pass
            await self._stop(set(self._tasks))
            if isinstance(original, _ForwardFatal):
                raise original.fatal.error from None
            raise
        try:
            await self.close()
        except _ForwardFatal as interrupted:
            raise interrupted.fatal.error from None
        finally:
            await self._stop(set(self._tasks))
        return result


async def run[T](what: str, work: Callable[[Owner], Awaitable[T]]) -> T:
    """Run native control with its existing opening, transaction and exit allowances."""
    owner = Owner()
    return await owner.run(lambda: owner._open(what), lambda: work(owner), opening_s=OPEN_S)


async def transaction[T](conn: Any, work: Callable[[], Awaitable[T]]) -> T:
    """Borrow the owner's backend; every failure discards it without a rollback wait."""
    tx = conn.transaction()
    await tx.start()
    await conn.execute("SET LOCAL lock_timeout = '5s'")
    await conn.execute("SET LOCAL statement_timeout = '10s'")
    await check_lock(conn)
    await conn.execute("SELECT pg_logical_emit_message(true, 'loom.control', '')")
    result = await work()
    await tx.commit()
    return result


async def request(
    url: Any,
    name: str,
    transport: Any,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    method: str = "POST",
) -> dict[str, Any]:
    """Read or mutate one operator row; gateways and lost replies are unknown outcomes."""
    import httpx

    from server.cli import cell as cli

    endpoint = cli.at(url, f"/cells/v1/operator/cells/{name}" + path)
    try:
        async with asyncio.timeout(REQUEST_S):
            manager = httpx.AsyncClient(
                timeout=cli._OPERATOR_TIMEOUT,
                trust_env=url.scheme == "https",
                transport=transport,
                headers={"Authorization": f"Bearer {settings.router_operator_token}"},
            )
            client = await manager.__aenter__()
            try:
                response = await client.request(method, endpoint, json=body)
            except BaseException:  # Client exit preserves the request's first failure.
                with contextlib.suppress(BaseException):
                    await manager.__aexit__(*sys.exc_info())
                raise
            else:
                await manager.__aexit__(None, None, None)
    except (httpx.HTTPError, TimeoutError) as e:
        raise _refused(f"router reply unknown ({type(e).__name__}): stop and rerun") from None
    answer = cli.router_answer(response, {"cell": name}, cli.OPERATOR_REFUSALS)
    if answer is None:
        raise _refused(cli.not_the_router(response))
    if response.status_code == 404 and answer.get("code") != "NO_SUCH_CELL":
        raise _refused("a router replica is not upgraded for cell control fencing")
    if response.status_code != 200:
        raise _refused(
            f"router refused {path or 'the read'} ({response.status_code}): {answer['error']}"
        )
    return answer


async def _acceptance(conn: Any) -> dict[str, Any]:
    return {
        "first": dict(row)
        if (row := await conn.fetchrow("SELECT is_first, cell FROM loom.cell_first"))
        else None,
        "mode": await conn.fetchval("SELECT mode FROM loom.tenancy_mode"),
        "own": dict(row)
        if (row := await conn.fetchrow("SELECT current_fp, next_fp FROM loom.cell_self"))
        else None,
    }


async def backout(owner: Owner, call: Call, row: dict[str, Any]) -> None:
    """The runbook's exact state cleanup, fenced against queued controllers/placements."""
    from server.core.schema import pg_migrate

    conn = owner.conn
    before = await owner.transaction(lambda: _acceptance(conn))
    first = before["first"]
    if first is not None and (first["cell"], first["is_first"]) != (row["cell"], row["is_first"]):
        raise _refused("the local first row names another registration")
    already_open = not any(before.values()) and not await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM loom.tenancy)"
    )
    if row["is_first"] and before["mode"] != "recording" and not already_open:
        raise _refused("first-cell backout requires local recording mode; enforced goes forward")
    row = await claim(conn, call, row)
    if row.get("state") != "attaching":
        raise _refused("a serving cell goes forward; state cleanup refused")
    # Closing and reading placed under one directory row lock excludes an ordinary
    # placement queued before the claim. A later failure deliberately leaves it closed.
    limited = await mutate(conn, call, row, "/limit", {"closed": True})
    if type(limited.get("placed_orgs")) is not int or limited["placed_orgs"] != 0:
        raise _refused("cell holds placements: cleanup refused; the cell remains closed")

    async def clear_state() -> None:
        await paired(conn, row)
        if json.dumps(await _acceptance(conn), sort_keys=True) != json.dumps(
            before, sort_keys=True
        ):
            raise _refused("local acceptance changed: cleanup refused; the cell remains closed")
        await conn.execute("SELECT pg_advisory_xact_lock($1)", pg_migrate._LOCK_KEY)
        await conn.execute("ALTER TABLE loom.cell_first DISABLE TRIGGER cell_first_immutable")
        for table in ("cell_first", "tenancy_mode", "cell_self", "tenancy"):
            await conn.execute(f"DELETE FROM loom.{table}")
        await conn.execute("ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_immutable")
        await conn.execute(
            "ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_no_truncate"
        )

    await owner.transaction(clear_state)

    # Once state is open, a migrating boot cannot recreate the org triggers. The same
    # session still holds ATTACH; a retry repeats the durable state cleanup first.
    async def clear_triggers() -> None:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", pg_migrate._LOCK_KEY)
        triggers = await conn.fetch(
            "SELECT t.tgname, c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'loom' "
            "AND t.tgname IN ('tenancy_insert', 'tenancy_update', 'tenancy_record')"
        )
        for trigger in triggers:
            name, table = (str(trigger[k]).replace('"', '""') for k in ("tgname", "relname"))
            await conn.execute(f'DROP TRIGGER "{name}" ON loom."{table}"')

    await owner.transaction(clear_triggers)
