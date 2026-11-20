"""Complete replica synchronization with the caller's existing scan budget."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from typing import Any

from clickhouse_connect.driver.exceptions import DatabaseError

from server.core.cell_ch import Refusal, Refused, Topology, quote
from server.core.ch_errors import error_code, error_line

_TIMEOUT_CODES = frozenset({159})  # TIMEOUT_EXCEEDED


class Budget:
    """`--scan-budget`: the seconds one full scan may take, shared by its statements."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = float(seconds)
        self._clock = clock
        self._start = clock()

    def elapsed(self) -> float:
        return self._clock() - self._start

    def left(self) -> float:
        return self.seconds - self.elapsed()

    def require(self, subject: str) -> float:
        """The seconds left, or Refused naming `subject` once none are."""
        left = self.left()
        if left <= 0:
            raise Refused([_out_of_budget(subject, self.seconds)])
        return left


def _out_of_budget(subject: str, seconds: float) -> Refusal:
    return Refusal(subject, f"the scan ran out of --scan-budget ({seconds:g} s): raise it")


def _on_cluster(topology: Topology) -> str:
    return f" ON CLUSTER {quote(topology.cluster)}" if topology.multi else ""


async def _system(ch: Any, sql: str, topology: Topology, budget: Budget, subject: str) -> None:
    """A SYSTEM statement bounded by what is left of `budget`. On several hosts it carries
    `ON CLUSTER` (`_on_cluster`) and each host's status is read back: every listed host must
    answer, each with status 0 (pinned with `distributed_ddl_output_mode = 'throw'`, so a
    profile's `never_throw` hides none). One that does not finish in time refuses as the
    budget when the budget is spent, else with the server's error and the hosts it did not
    finish on: a replica that cannot sync."""
    seconds = max(1, int(budget.require(subject)))
    # `receive_timeout` bounds SYNC REPLICA's own wait.
    bounds = {"max_execution_time": seconds, "receive_timeout": seconds}
    what = sql.split(" ON CLUSTER ")[0].split(" `")[0]
    try:
        if not topology.multi:
            await ch.execute(sql, settings=bounds)
            return
        body = await ch.raw_bytes(
            sql,
            settings={
                **bounds,
                "distributed_ddl_output_mode": "throw",
                "distributed_ddl_task_timeout": seconds,
                "default_format": "JSONEachRow",
            },
        )
        rows = _body_rows(body.decode("utf-8", "replace").splitlines(), "status")
    except Exception as e:  # only a timeout is judged here; the rest pass to the caller
        if error_code(e) not in _TIMEOUT_CODES:
            raise
        # The statement was given the budget's whole seconds, so up to one is left over.
        if budget.left() <= 1:
            raise Refused([_out_of_budget(subject, budget.seconds)]) from None
        done = {str(r.get("host")) for r in getattr(e, "rows", ()) if str(r.get("status")) == "0"}
        unfinished = ", ".join(sorted(topology.hosts - done)) or "this server"
        raise Refused(
            [Refusal(subject, f"{what} did not finish on {unfinished}: {error_line(e)}")]
        ) from e
    failed = [r for r in rows if int(r["status"]) != 0]
    if failed:
        raise Refused(
            [Refusal(f"replica {r.get('host')}", f"{subject}: {r.get('error')}") for r in failed]
        )
    answered = {str(r.get("host")) for r in rows}
    if answered != topology.hosts:
        missing = ", ".join(sorted(topology.hosts - answered)) or "none"
        raise Refused(
            [
                Refusal(
                    subject, f"the hosts that answered are not the listed ones (missing: {missing})"
                )
            ]
        )


class BodyError(DatabaseError):
    """An error ClickHouse wrote into a response body after it began streaming rows: a
    server error as the driver raises one, its text keeping the server's `Code: <n>.`, so it
    is judged transient or not, and by code, as any other. `rows` are the rows before it."""

    def __init__(self, text: str, rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__(text)
        self.rows = list(rows or [])


def _body_rows(lines: Iterable[str], key: str) -> list[dict[str, Any]]:
    """The JSONEachRow rows of a body, each holding `key`; BodyError at the first line that
    is not one (an `{"exception": …}` row, or the text tail a server appends once a
    statement fails after its first block), with the rest of the body as its text."""
    lines = list(lines)
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict) or key not in row:
            exception = row.get("exception") if isinstance(row, dict) else None
            raise BodyError(str(exception or "\n".join(lines[i:]))[:2000], rows)
        rows.append(row)
    return rows
