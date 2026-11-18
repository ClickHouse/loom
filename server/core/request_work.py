"""Per-request work bag: what one request cost in ClickHouse, where its wall
went, and which legs it could not run.

`agent_events` says how long a request took and what its LLM calls cost.
This bag adds what the request read from ClickHouse, how a `memory.ask`
split its wall between retrieval and synthesis, and which legs were skipped
because the service lacks a table this build reads — facts that otherwise
sit in `system.query_log` on whichever replica ran the statement and in log
lines, one of them not readable by the loom login.

This module is the request-scoped place those facts accumulate. The
transport binds a fresh bag at op start (next to the cost accumulator and the
degradation notes); the ClickHouse client, the ask service and the schema
guard write into it; `event_args.request_work_columns` flattens it onto the
ledger row. It also carries one authorization outcome the row's
`status_class` reads: `answered_unverified`, which `server.auth.fail_closed`
sets when the request hid a row because a read the check needed failed.
Like the cost accumulator, nothing is recorded when no bag is bound (a
worker pass, a test) — a long-lived task never accumulates across requests.
Accounting helpers are best effort. A new marked-event claim refuses without
a marked logical call and a live required request tail.
"""

from __future__ import annotations

import contextvars
import secrets
import time
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from server.core.diagnostics import codes_only

_MAX_SKIPPED = 16


@dataclass
class RequestWork:
    began: float = field(default_factory=time.perf_counter)
    last_mark: float = field(default_factory=time.perf_counter)
    # Ask-side stage partition: wall charged to named stages
    # from one origin, the way `StageClock` does for memory.search.
    stage_ms: dict[str, float] = field(default_factory=dict)
    # ClickHouse cost from the response summary header, summed per request.
    ch_statements: int = 0
    ch_read_rows: int = 0
    ch_read_bytes: int = 0
    ch_elapsed_ms: float = 0.0
    # For `query_id = <request_id>:<n>:<nonce>`: the nonce keeps two requests
    # with one caller id from sending a query_id ClickHouse is still running.
    ch_seq: int = 0
    ch_nonce: str = field(default_factory=lambda: secrets.token_hex(4))
    # Identifier injection: rows the exact reads put in the
    # synthesis pool, facts and verbatim chunks.
    injected_facts: int = 0
    injected_chunks: int = 0
    # Schema guard: tables a pending migration creates that a
    # leg of this request tried to read; the leg continued without them.
    schema_skipped: list[str] = field(default_factory=list)
    # The request answered as if refused because a read the check needed
    # failed (`row_in_scope`'s found=False; server.auth.fail_closed): its
    # row is the server's.
    answered_unverified: bool = False
    # The (org, namespace) the gate admitted this call against, defaults
    # resolved (a code op naming none runs in the code namespace): the ledger
    # row's tenant where the request named none.
    admitted: tuple[str, str] | None = None
    # Mutable with the bag: a child leg marks its parent request even when it admits
    # nothing. Diagnostic suppression must not hide this authorization state.
    mirror_marked: bool = False
    mirror_unread: bool = False
    mirror_event_registered: bool = False

    def mark(self, name: str) -> float:
        now = time.perf_counter()
        ms = (now - self.last_mark) * 1000.0
        self.last_mark = now
        self.stage_ms[name] = round(float(self.stage_ms.get(name, 0.0)) + ms, 1)
        return ms

    def partition(self) -> dict[str, float]:
        """The stages marked so far plus `total` (wall since the bag was
        bound); empty when nothing was marked."""
        if not self.stage_ms:
            return {}
        out = dict(self.stage_ms)
        out["total"] = round((time.perf_counter() - self.began) * 1000.0, 1)
        return out


_current: contextvars.ContextVar[RequestWork | None] = contextvars.ContextVar(
    "loom_request_work", default=None
)


_http_owner: contextvars.ContextVar[RequestWork | None] = contextvars.ContextVar(
    "loom_http_request_work_owner", default=None
)
_HTTP_OWNER_KEY = "loom.request_work_owner"


@contextmanager
def http_owner(scope: MutableMapping[str, Any]) -> Iterator[None]:
    """Own HTTP marks across middleware tasks and fresh logical operation bags."""
    owner = RequestWork()
    scope[_HTTP_OWNER_KEY] = owner
    owner_token = _http_owner.set(owner)
    local_token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(local_token)
        _http_owner.reset(owner_token)


def http_mirror_marked(scope: Mapping[str, Any] | None = None) -> bool:
    owner = _http_owner.get() if scope is None else scope.get(_HTTP_OWNER_KEY)
    return isinstance(owner, RequestWork) and owner.mirror_marked


def http_mirror_unread(scope: Mapping[str, Any] | None = None) -> bool:
    owner = _http_owner.get() if scope is None else scope.get(_HTTP_OWNER_KEY)
    return isinstance(owner, RequestWork) and owner.mirror_unread


def begin() -> RequestWork:
    """Bind a fresh bag to the current context and return it."""
    bag = RequestWork()
    _current.set(bag)
    return bag


@contextmanager
def bind(bag: RequestWork) -> Iterator[None]:
    """Restore a logical owner's bag when a stream closes in another context."""
    token = _current.set(bag)
    try:
        yield
    finally:
        _current.reset(token)


def current() -> RequestWork | None:
    return None if codes_only() else _current.get()


def admitted_scope() -> tuple[str, str] | None:
    """Keep verified ledger attribution available when diagnostics hide the bag."""
    bag = _current.get()
    return bag.admitted if bag is not None else None


def mark_mirrored(*, unread: bool = False) -> bool:
    """Mark a leg; False means no outer request bag exists and it must serve no mirror."""
    bag = _current.get()
    if bag is None:
        return False
    bag.mirror_marked = True
    bag.mirror_unread |= unread
    owner = _http_owner.get()
    if owner is not None:
        owner.mirror_marked = True
        owner.mirror_unread |= unread
    return True


def mirror_marked() -> bool:
    bag = _current.get()
    return bag is not None and bag.mirror_marked


def flag_response(body: dict[str, Any]) -> dict[str, Any]:
    """Place the server-owned privacy flag before a marked logical call's fields."""
    if not mirror_marked():
        return body
    return {"mirrored": True, **{k: v for k, v in body.items() if k != "mirrored"}}


def marked_event_pending() -> bool:
    """A raw logical mark still owes its one required event registration."""
    bag = _current.get()
    return bag is not None and bag.mirror_marked and not bag.mirror_event_registered


def claim_marked_event() -> bool:
    """Claim once; a new claim requires a marked call and a live required tail.

    Raise RequiredTailError when a new claim lacks either lifetime.
    """
    from server.core import request_tail

    bag = _current.get()
    if bag is None or not bag.mirror_marked:
        raise request_tail.RequiredTailError()
    if bag.mirror_event_registered:
        return False
    if not request_tail.required_active():
        raise request_tail.RequiredTailError()
    bag.mirror_event_registered = True
    return True


def mark_stage(name: str) -> None:
    """Charge the wall since the previous mark (or the bag's start) to
    `name`. No bag bound: nothing happens."""
    bag = current()
    if bag is None:
        return
    try:
        bag.mark(str(name))
    except Exception:  # noqa: BLE001 — telemetry never fails a request
        pass


def note_ch_summary(summary: Any) -> dict[str, Any]:
    """Fold one statement's response summary (`X-ClickHouse-Summary`:
    `read_rows`, `read_bytes`, `elapsed_ns`, `query_id`) into the bound bag.
    Returns the parsed numbers (`read_rows`, `read_bytes`, `elapsed_ms`,
    `query_id`) for the caller's span, or {} when the summary is empty or
    unreadable — a Cloud response without the header is a null reading, not
    an error. Never raises."""
    if codes_only():
        return {}
    try:
        if not isinstance(summary, dict) or not summary:
            return {}
        rows = _int(summary.get("read_rows"))
        nbytes = _int(summary.get("read_bytes"))
        elapsed_ns = _int(summary.get("elapsed_ns"))
        qid = str(summary.get("query_id") or "")
        if rows is None and nbytes is None and elapsed_ns is None:
            return {}
        parsed: dict[str, Any] = {
            "read_rows": rows or 0,
            "read_bytes": nbytes or 0,
            "elapsed_ms": round((elapsed_ns or 0) / 1e6, 3),
        }
        if qid:
            parsed["query_id"] = qid
        bag = current()
        if bag is not None:
            bag.ch_statements += 1
            bag.ch_read_rows += parsed["read_rows"]
            bag.ch_read_bytes += parsed["read_bytes"]
            bag.ch_elapsed_ms += parsed["elapsed_ms"]
        return parsed
    except Exception:  # noqa: BLE001 — an unreadable summary is a null reading
        return {}


def next_query_seq() -> tuple[int, str] | None:
    """The request's next statement number and its nonce, or None when no bag is
    bound (a worker pass keeps the driver's own query ids)."""
    bag = current()
    if bag is None:
        return None
    bag.ch_seq += 1
    return bag.ch_seq, bag.ch_nonce


def note_injected(*, facts: int = 0, chunks: int = 0) -> None:
    bag = current()
    if bag is None:
        return
    bag.injected_facts += max(0, int(facts))
    bag.injected_chunks += max(0, int(chunks))


def note_schema_skipped(table: str) -> None:
    bag = current()
    if bag is None or not table:
        return
    name = str(table)
    if name not in bag.schema_skipped and len(bag.schema_skipped) < _MAX_SKIPPED:
        bag.schema_skipped.append(name)


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "RequestWork",
    "admitted_scope",
    "begin",
    "current",
    "mark_stage",
    "next_query_seq",
    "note_ch_summary",
    "note_injected",
    "note_schema_skipped",
]
