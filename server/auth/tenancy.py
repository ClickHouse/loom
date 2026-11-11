"""The tenancy check: a cell serves only the orgs it holds (docs/architecture/cells.md,
"The tenancy check and placement").

With `LOOM_CELL` unset nothing here runs. With it set, the transports call `ensure_held`
for the org a verified call acts in, before anything is written:

- a principal that may not act in the org (a token of another org) is refused first,
  as the gate would refuse it, so it records, places and learns nothing;
- an org the cell holds (a `loom.tenancy` row, or one this process has already seen
  held: a held org is never wrong to serve) passes with no further read;
- in `recording` (the first cell before its attach finishes) the org is recorded by
  one statement that inserts its row only while the mode is still `recording`, and only
  while the directory answers that this cell is `attaching` (`GET /cells/v1/cells/<cell>`,
  read at most every LOOM_TENANCY_MODE_CACHE_S); while it is unreadable or answers
  anything else, a new org answers 503 and the orgs the cell holds keep serving;
- in `enforced`, a principal scoped to the org asks the router to place it (insert,
  then read the winner back): placed here, the cell writes its row and serves; placed
  elsewhere, the call answers 409 `WRONG_CELL` and nothing is written. A platform or
  `*`-org principal never places an org. A failed placement call answers 503.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import TYPE_CHECKING

from server.core.errors import BackendUnavailableError, WrongCellError, caller_text
from server.settings import settings

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo

log = logging.getLogger(__name__)

# Orgs that name no tenant: deployment-wide rows and the token defaults.
ORG_FREE = frozenset({"", "-", "*"})

# Orgs this process has seen held. Only grows: a tenancy row is never deleted.
_held: set[str] = set()
# Orgs the router answered are placed in another cell, and where: a placement never
# changes, so a client calling the wrong cell costs no router call after the first.
# Bounded: past the bound the oldest is forgotten and asked for again.
_ORG_CACHE_MAX = 10_000  # not a setting: a memory bound on each per-org cache here
_elsewhere: dict[str, str] = {}
# Orgs `is_held` read as not held, and when (monotonic): read again after
# LOOM_TENANCY_MODE_CACHE_S, so a caller acting in another org reads it once a window.
_not_held: dict[str, float] = {}
# The directory's answer to "is this cell `attaching`?" and when it was read (monotonic),
# and the read running now: a burst of new orgs shares one router call per window.
_attaching: tuple[bool, float] | None = None
_attaching_read: asyncio.Task[bool] | None = None
# The placement running in this process for each org: requests arriving together for a
# new org share one router call.
_placing: dict[str, asyncio.Task[tuple[str, str]]] = {}

_HOLDS = "SELECT 1 FROM loom.tenancy WHERE org = $1"
# Records the org only while the mode is `recording`; the mode row is read FOR SHARE, so
# attach's write of `enforced` waits for a running insert and a later insert reads it.
_RECORD = (
    "INSERT INTO loom.tenancy (org) SELECT $1::text FROM loom.tenancy_mode "
    "WHERE mode = 'recording' FOR SHARE ON CONFLICT (org) DO NOTHING RETURNING 1"
)
_ADD = "INSERT INTO loom.tenancy (org) VALUES ($1) ON CONFLICT (org) DO NOTHING"


def attributable(org: str) -> bool:
    """Whether a request's telemetry may name `org`: always outside a cell; in one,
    only an org this process has seen held, so a call refused for another cell's org,
    or before its org was checked, leaves no row keyed by that org here."""
    return not settings.cell or org in ORG_FREE or org in _held


async def is_held(org: str) -> bool:
    """Whether this cell holds `org`, read from its row on a cache miss, placing and
    recording nothing; an unread row counts as not held, and a not-held answer may be up
    to LOOM_TENANCY_MODE_CACHE_S old."""
    if not settings.cell or org in ORG_FREE or org in _held:
        return True
    read_at = _not_held.get(org)
    if read_at is not None and time.monotonic() - read_at < settings.tenancy_mode_cache_s:
        return False
    from server.core import pg

    try:
        held = bool(await pg.fetchval(_HOLDS, org))
    except Exception as e:  # noqa: BLE001 — not known held: the caller does nothing for it
        log.warning("tenancy: %s unread: %s", org, e)
        return False
    if held:
        _held.add(org)
        _not_held.pop(org, None)
    else:
        _not_held.pop(org, None)  # re-read: it goes to the back, by its last read
        if len(_not_held) >= _ORG_CACHE_MAX:
            _not_held.pop(next(iter(_not_held)))
        _not_held[org] = time.monotonic()
    return held


def scoped_to(auth: AuthInfo | None, org: str) -> bool:
    """Whether a verified principal is scoped to `org`: its token's own org. A
    platform or `*`-org principal acts in orgs the cell holds and places none."""
    from server.operations.base import Role

    if auth is None or auth.org != org or org in ORG_FREE:
        return False
    return not auth.has_role(Role.PLATFORM_ADMIN)


async def ensure_held(org: str | None, auth: AuthInfo | None) -> None:
    """Pass when this cell holds `org`, recording or placing it as the mode allows.
    Raise `PermissionError` (403) first for a principal that may not act in the org,
    `WrongCellError` (409) for another cell's org, `BackendUnavailableError` (503)
    when the cell cannot tell. Nothing is written for an org refused."""
    if not settings.cell or org is None or org in ORG_FREE:
        return
    # A principal that may not act in the org is refused first, as the gate would, so
    # it records, places and learns nothing: not whether this cell holds the org, nor
    # where it is placed.
    from server.auth.authz import may_act_in_org

    if auth is not None and not may_act_in_org(auth, org):
        raise PermissionError("token is for another org")
    if org in _held:
        return
    if org in _elsewhere:
        raise WrongCellError(org, cell=_elsewhere[org])
    from server.core import cell, pg

    try:
        held = bool(await pg.fetchval(_HOLDS, org))
    except Exception as e:  # re-raised as 503: an unread tenancy refuses, never serves
        raise _unread(org, e) from e
    if held:
        _held.add(org)
        return
    if cell.mode() == "recording":
        # A restore to before attach's step 3 whose `loom cell restore` was skipped reads
        # `recording` too: the directory, which says `serving` then, tells the two apart.
        if not await directory_says_attaching():
            missing = _router_unset()
            why = f"{', '.join(missing)} unset" if missing else "unreadable or serving now"
            raise BackendUnavailableError(
                f"org {org!r} is new to this cell, which records new orgs only while the "
                f"directory answers that it is attaching ({why})",
                operation="tenancy",
            )
        try:
            # Recorded now, or by another process a moment ago (the insert then conflicts).
            if await pg.fetchval(_RECORD, org) or await pg.fetchval(_HOLDS, org):
                _held.add(org)
                return
        except Exception as e:  # re-raised as 503: an unread tenancy refuses, never serves
            raise _unread(org, e) from e
    # Not held, and either `enforced` or the mode moved on since the cached read:
    # placement decides. A configured org a call with no principal acts in (JIT,
    # self-signup) is placed by attach, or by `loom cell update` when it changes: retry.
    if auth is None and org in cell.mine()["configured_orgs"]:
        raise BackendUnavailableError(
            f"org {org!r} is configured for this cell and not placed yet (attach places it, "
            "or `loom cell update` once the cell serves)",
            operation="tenancy",
        )
    if not scoped_to(auth, org):
        raise WrongCellError(org)
    winner, deployment_id = await _place_once(org)
    if winner != settings.cell:
        if len(_elsewhere) >= _ORG_CACHE_MAX:
            _elsewhere.pop(next(iter(_elsewhere)))
        _elsewhere[org] = winner
        raise WrongCellError(org, cell=winner)
    if deployment_id != cell.deployment_id():
        # This cell's name, another deployment's stores: the directory's record of
        # the cell is not this process's. Nothing is written; the operator looks.
        raise BackendUnavailableError(
            f"the router places org {org!r} in cell {winner!r} under another deployment "
            "id than this process verified; see loom doctor",
            operation="placement",
        )
    try:
        await pg.execute(_ADD, org)
    except Exception as e:  # re-raised as 503: the placement stands; the row lands on retry
        log.warning("tenancy: %s placed here, row unwritten: %s", org, e)
        raise BackendUnavailableError(
            f"org {org!r} is placed in this cell but its row is not written yet: {caller_text(e)}",
            operation="tenancy",
        ) from e
    _held.add(org)


def _unread(org: str, e: Exception) -> BackendUnavailableError:
    log.warning("tenancy: %s unread: %s", org, e)
    return BackendUnavailableError(
        f"this cell cannot tell whether it holds org {org!r}: {caller_text(e)}",
        operation="tenancy",
    )


def _router_url() -> str:
    return settings.cell_router_url.rstrip("/")


def _router_unset() -> list[str]:
    """The router settings a call to it needs and does not have."""
    values = (
        ("LOOM_CELL_ROUTER_URL", _router_url()),
        ("LOOM_CELL_ROUTER_TOKEN", settings.cell_router_token),
    )
    return [name for name, value in values if not value]


async def directory_says_attaching() -> bool:
    """Whether the directory answers that this cell is `attaching`, read at most every
    LOOM_TENANCY_MODE_CACHE_S and shared by the requests waiting on it; a failed,
    refused or late answer is False and stands for the window too."""
    global _attaching, _attaching_read
    if _attaching is not None and time.monotonic() - _attaching[1] < settings.tenancy_mode_cache_s:
        return _attaching[0]
    if _attaching_read is None or _attaching_read.done():
        _attaching_read = asyncio.create_task(_read_attaching())
        # Retrieved here too: every waiter may have gone.
        _attaching_read.add_done_callback(lambda t: t.cancelled() or t.exception())
    return await asyncio.shield(_attaching_read)


async def _read_attaching() -> bool:
    global _attaching
    answer = await _ask_attaching()
    _attaching = (answer, time.monotonic())
    return answer


async def _ask_attaching() -> bool:
    url = _router_url()
    if missing := _router_unset():
        log.warning("cell state: no router to ask (%s unset)", ", ".join(missing))
        return False
    import httpx

    limit = float(settings.cell_router_timeout_s)
    try:
        async with asyncio.timeout(limit), httpx.AsyncClient(timeout=limit) as c:
            r = await c.get(
                f"{url}/cells/v1/cells/{settings.cell}",
                headers={"Authorization": f"Bearer {settings.cell_router_token}"},
            )
        answer = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError) as e:
        log.warning("cell state: the router did not answer: %s", type(e).__name__)
        return False
    if r.status_code != 200:
        log.warning("cell state: the router answered %s", r.status_code)
    return isinstance(answer, dict) and answer.get("state") == "attaching"


async def _place_once(org: str) -> tuple[str, str]:
    """`_place`, shared by the requests of this process waiting on the same org; one
    request cancelled leaves the call running for the others."""
    task = _placing.get(org)
    if task is None:
        task = asyncio.create_task(_place(org))
        _placing[org] = task
        task.add_done_callback(functools.partial(_placed, org))
    return await asyncio.shield(task)


def _placed(org: str, task: asyncio.Task[tuple[str, str]]) -> None:
    _placing.pop(org, None)
    if not task.cancelled():
        task.exception()  # retrieved: every waiter may have gone


async def _place(org: str) -> tuple[str, str]:
    """Ask the router to place `org` in this cell; the (cell, deployment id) that holds
    it. The router inserts the placement if absent and reads the winner back, so a
    retry is safe."""
    from server.core import cell

    url = _router_url()
    if missing := _router_unset():
        raise BackendUnavailableError(
            f"this cell has no router to place a new org with ({', '.join(missing)} unset)",
            operation="placement",
        )
    import httpx

    body = {"org": org, "cell": settings.cell, "fingerprint": cell.as_value(cell.mine())}
    limit = float(settings.cell_router_timeout_s)
    try:
        # The whole call, not each read: a router trickling its answer is cut off too.
        async with asyncio.timeout(limit), httpx.AsyncClient(timeout=limit) as c:
            r = await c.post(
                f"{url}/cells/v1/placements",
                json=body,
                headers={"Authorization": f"Bearer {settings.cell_router_token}"},
            )
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError) as e:
        log.warning("placement of %s: router unreachable: %s", org, e)
        raise BackendUnavailableError(
            f"the router did not answer the placement of org {org!r}", operation="placement"
        ) from e
    if r.status_code != 200:
        log.warning("placement of %s: router answered %s", org, r.status_code)
        raise BackendUnavailableError(
            f"the router refused the placement of org {org!r} ({r.status_code})",
            operation="placement",
            retry_after_s=_retry_after(r.headers.get("retry-after")),
        )
    unnamed = BackendUnavailableError(
        f"the router's placement answer for org {org!r} names no cell", operation="placement"
    )
    try:
        answer = r.json()
    except ValueError:
        raise unnamed from None
    if not isinstance(answer, dict):
        raise unnamed
    winner, deployment_id = answer.get("cell"), answer.get("deployment_id")
    if not isinstance(winner, str) or not winner or not isinstance(deployment_id, str):
        raise unnamed
    return winner, deployment_id


def _retry_after(value: str | None) -> int:
    """The router's Retry-After in whole seconds, 1 to 60 (its rate's window); 1 when
    it sends none or one this cell cannot read."""
    try:
        return min(max(int(value or 1), 1), 60)
    except ValueError:
        return 1
