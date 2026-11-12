"""`loom cell prepare`: register this cell in the router's directory and write the rows its
processes read (docs/architecture/cells.md, "Preparing a cell" and "The tenancy check").

It runs where the cell's stores are configured (`LOOM_CELL` set to the cell it names):

1. It reads the cell's Postgres (deployment id, `first`, mode and own rows, tenancy rows)
   and the live processes' heartbeats, and refuses a cell whose rows name another cell
   or a process it cannot take on its own (no heartbeat row, another `LOOM_CELL`, other
   stores).
2. It writes a nonce into the cell's Postgres, ClickHouse and bucket, and asks the router
   to register the cell; the router reads the nonces back through the cell's probe
   listener before it records anything. A busy or unreachable listener is retried within
   `--wait`.
3. It deletes the nonces, checks that every live process runs a configuration the cell
   accepts (judged from the router's answer: the directory's once attach wrote it), and
   writes the missing `first` and mode rows and the own row where it differs, in one
   Postgres transaction.

A rerun is safe at any point: the router answers the row it holds.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

from server.cli import cell as cell_cli
from server.core import cell
from server.settings import settings

# How long a run retries a busy or unreachable probe listener by default.
_WAIT_S = 600  # not a setting: a flag's default

# The longest --wait: the probe's Keeper key lives that long (and a stopped run's is swept
# as a dead lease key past it), so a run never waits on a key the sweep took.
_WAIT_MAX_S = 86400  # not a setting: a flag's bound


_sleep = asyncio.sleep  # the seam the retry tests replace


class Refused(Exception):
    """Prepare stops here; the message says what happened."""


@dataclass
class _CellRows:
    """What the cell's Postgres holds of its own state."""

    deployment_id: str
    first: tuple[bool, str] | None
    mode: str
    current: dict[str, Any] | None
    next: dict[str, Any] | None
    tenancy_rows: int


@dataclass
class _Beat:
    """One live process: the `LOOM_CELL` it runs with and its fingerprint's stored form
    (None with no heartbeat row, as a build before cells writes none, or an unreadable
    one)."""

    instance_id: str
    cell: str
    fingerprint: dict[str, Any] | None


def _args_problem(args: argparse.Namespace) -> str:
    if not cell.NAME.fullmatch(args.cell or ""):
        return f"{args.cell!r} is no cell name (1 to 32 of a-z, 0-9, -)"
    if args.cell != settings.cell:
        return (
            f"LOOM_CELL is {settings.cell!r}: prepare runs with the cell's own configuration, "
            f"LOOM_CELL={args.cell}"
        )
    if settings.storage_authority != "postgres" or not settings.pg_dsn:
        return (
            "a cell keeps its state in Postgres: set LOOM_STORAGE_AUTHORITY=postgres and "
            "LOOM_PG_DSN"
        )
    if not cell.normalise_host(args.host or ""):
        return f"{args.host!r} is no host"
    if not cell.SHA256.fullmatch(args.credential_sha256 or ""):
        return (
            "--credential-sha256 takes the cell credential's sha256 (64 lowercase hex), never "
            "the credential: `loom cell credential new` prints both"
        )
    if not 0 <= args.wait <= _WAIT_MAX_S:
        return f"--wait is a number of seconds from 0 to {_WAIT_MAX_S}"
    return ""


async def _read_rows() -> _CellRows:
    from server.core import pg, pg_profile

    deployment_id = await pg_profile.deployment_id()
    first = await pg.fetchrow("SELECT is_first, cell FROM loom.cell_first")
    mode = await pg.fetchval("SELECT mode FROM loom.tenancy_mode")
    own = await pg.fetchrow("SELECT current_fp, next_fp FROM loom.cell_self")
    tenancy_rows = await pg.fetchval("SELECT count(*) FROM loom.tenancy")
    return _CellRows(
        deployment_id=deployment_id,
        first=None if first is None else (bool(first["is_first"]), str(first["cell"])),
        mode=str(mode or ""),
        current=None if own is None else cell.json_object(own["current_fp"]),
        next=None if own is None else cell.json_object(own["next_fp"]),
        tenancy_rows=int(tenancy_rows or 0),
    )


async def _read_beats(ch: Any) -> list[_Beat]:
    """Every live process sharing this ClickHouse, with its heartbeat row if it wrote one."""
    from server.core import instances

    live = await instances.read_live(ch, all_deployments=True)
    rows = await ch.query_all(
        f"SELECT instance_id, cell, fingerprint FROM {instances.CELL_TABLE} "
        "WHERE has({ids:Array(String)}, instance_id)",
        {"ids": [str(r["instance_id"]) for r in live]},
    )
    reported = {str(r["instance_id"]): r for r in rows}
    beats = []
    for r in live:
        row = reported.get(str(r["instance_id"]))
        beats.append(
            _Beat(
                instance_id=str(r["instance_id"]),
                cell="" if row is None else str(row["cell"]),
                fingerprint=None if row is None else _fingerprint(str(row["fingerprint"])),
            )
        )
    return beats


def _fingerprint(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


_STORE_FIELDS = ("deployment_id", "bucket", "bucket_prefix")


def _beat_fields(beats: list[_Beat], rows: _CellRows) -> list[dict[str, Any]]:
    """Each live process's fingerprint fields, or Refused naming a process this cell
    cannot take: one with no readable heartbeat row, another `LOOM_CELL`, or other stores."""
    if not beats:
        raise Refused("no live process of this cell reports a heartbeat: start the cell first")
    missing = [b.instance_id for b in beats if b.fingerprint is None]
    if missing:
        raise Refused(
            f"live process(es) {', '.join(missing[:5])} report no readable heartbeat row: a build "
            "before cells (upgrade it), or one that just started (rerun shortly)"
        )
    elsewhere = [b.instance_id for b in beats if b.cell and b.cell != settings.cell]
    if elsewhere:
        raise Refused(
            f"live process(es) {', '.join(elsewhere[:5])} run with another LOOM_CELL on these "
            "stores"
        )
    mine = cell.fingerprint(rows.deployment_id)
    stores = tuple(mine[k] for k in _STORE_FIELDS)
    out = []
    for b in beats:
        value = b.fingerprint
        assert value is not None  # refused above
        fields = value.get("fields")
        if value.get("v") != cell.FINGERPRINT_VERSION or not isinstance(fields, dict):
            raise Refused(
                f"live process {b.instance_id} reports a fingerprint this build cannot read"
            )
        # A field the process does not report is at its default, as `cell.matches` reads it.
        served = tuple(fields.get(k, cell.DEFAULTS[k]) for k in _STORE_FIELDS)
        if served != stores:
            raise Refused(
                f"live process {b.instance_id} serves another deployment id, bucket or prefix "
                "than this command's configuration"
            )
        out.append(fields)
    return out


def _state_rows(rows: _CellRows) -> int:
    """How many of the `first`, mode and own rows the cell's Postgres holds."""
    return (rows.first is not None) + bool(rows.mode) + (rows.current is not None)


async def _prefix_objects() -> int:
    """Objects under the cell's bucket prefix other than the probe's (0 with no bucket),
    counted up to two."""
    from server.core import cell_probe, object_store

    if not settings.document_s3_bucket:
        return 0
    page = await object_store.list_page(
        settings.document_s3_bucket, object_store.root_prefix(), max_keys=2
    )
    probe = object_store.root_prefix() + cell_probe.OBJECT_NAME
    return sum(1 for o in page.objects if o.key != probe)


def _new_nonces() -> dict[str, str | None]:
    return {
        "postgres": secrets.token_hex(16),
        "clickhouse": secrets.token_hex(16),
        "bucket": secrets.token_hex(16) if settings.document_s3_bucket else None,
    }


async def _write_nonces(ch: Any, nonces: dict[str, str | None]) -> None:
    from server.core import cell_probe, leases, object_store, pg

    await pg.execute(
        "INSERT INTO loom.cell_probe (name, nonce) VALUES ($1, $2) "
        "ON CONFLICT (name) DO UPDATE SET nonce = EXCLUDED.nonce, written_at = now()",
        cell_probe.NONCE_NAME,
        nonces["postgres"],
    )
    # A key left by a stopped run is replaced (a KeeperMap insert replaces the key): the
    # probe key is prepare's alone.
    await ch.execute(
        f"INSERT INTO {leases.LOCK_TABLE} (name, holder, acquired_at, renewed_at, ttl_s, "
        "build_commit) SETTINGS keeper_map_strict_mode = 0, async_insert = 0 "
        "VALUES ({name:String}, {nonce:String}, now64(3), now64(3), {ttl:UInt32}, '')",
        {"name": cell_probe.NONCE_NAME, "nonce": nonces["clickhouse"], "ttl": _WAIT_MAX_S},
    )
    if nonces["bucket"] is not None:
        data = nonces["bucket"].encode()
        loc = object_store.Location(
            settings.document_s3_bucket, object_store.root_prefix() + cell_probe.OBJECT_NAME
        )
        async with object_store.transfer() as lease:
            await object_store.put(loc, data, hashlib.sha256(data).hexdigest(), lease)


async def _delete_nonces(ch: Any, nonces: dict[str, str | None]) -> None:
    """Delete each nonce only while it holds this run's value; one that stays is named.
    The bucket object is read, then deleted, so a run that replaced it in between loses
    its object and is rerun."""
    from server.core import cell_probe, leases, object_store, pg

    name = cell_probe.NONCE_NAME

    async def _postgres() -> None:
        await pg.execute(
            "DELETE FROM loom.cell_probe WHERE name = $1 AND nonce = $2", name, nonces["postgres"]
        )

    async def _clickhouse() -> None:
        # A lease's release: strict mode makes it a compare-and-delete on the key's version.
        await ch.execute(leases.LOCK_RELEASE, {"name": name, "me": nonces["clickhouse"]})

    async def _bucket() -> None:
        loc = object_store.Location(
            settings.document_s3_bucket, object_store.root_prefix() + cell_probe.OBJECT_NAME
        )
        data = await object_store.read(loc)
        if data is not None and data.decode("utf-8", "replace") == nonces["bucket"]:
            await object_store.delete(loc.bucket, [loc.key])

    steps = [("Postgres", _postgres), ("ClickHouse", _clickhouse)]
    if nonces["bucket"] is not None:
        steps.append(("the bucket", _bucket))
    for where, step in steps:
        try:
            await step()
        except Exception as e:  # noqa: BLE001 — every nonce is tried; a stale one holds no data
            print(f"loom cell prepare: the probe nonce in {where} stays: {e}", file=sys.stderr)


def _register_timeout() -> httpx.Timeout:
    from server.router.app import REGISTER_ANSWER_MAX_S

    # The router's own bound on a registration's answer, and a margin.
    return httpx.Timeout(REGISTER_ANSWER_MAX_S + 10.0, connect=5.0)


async def _register(
    args: argparse.Namespace,
    router: httpx.URL,
    body: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    """The router's 200 answer, retrying a busy or unreachable probe listener within
    `--wait`; Refused otherwise."""
    token = settings.router_operator_token
    url = cell_cli.at(router, f"/cells/v1/operator/cells/{args.cell}/register")
    deadline = time.monotonic() + args.wait
    async with httpx.AsyncClient(
        timeout=_register_timeout(), trust_env=router.scheme == "https", transport=transport
    ) as client:
        while True:
            try:
                r = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                # A rerun answers the row a lost answer may have written.
                raise Refused(
                    f"no answer from the router ({type(e).__name__}): run prepare again"
                ) from None
            answer = cell_cli.router_answer(r, {"cell": args.cell}, cell_cli.OPERATOR_REFUSALS)
            if answer is None:
                raise Refused(
                    f"{cell_cli.not_the_router(r)}; run prepare again once the router answers"
                )
            if r.status_code == 200:
                return answer
            code = answer.get("code")
            if code == "BACKEND_UNAVAILABLE":
                raise Refused(
                    f"the directory did not answer ({answer['error']}); the registration may "
                    "have landed: run prepare again"
                )
            if code not in ("PROBE_BUSY", "PROBE_UNREACHABLE"):
                raise Refused(f"the router refused ({r.status_code}): {answer['error']}")
            retry_after = r.headers.get("Retry-After", "")
            pause = (
                max(1.0, float(retry_after))  # never 0: a gateway's would spin
                if code == "PROBE_BUSY" and retry_after.isascii() and retry_after.isdigit()
                else 2.0
            )
            if time.monotonic() + pause > deadline:
                raise Refused(
                    f"the router could not probe the cell within --wait: {answer['error']}"
                )
            print(f"loom cell prepare: {answer['error']}; retrying", file=sys.stderr)
            await _sleep(pause)


def _own_rows(
    rows: _CellRows, answer: dict[str, Any], every: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The `current` and `next` values the cell's own row should hold, from the directory
    once attach wrote them, else the own row, else the processes' one configuration; each
    live process (`every`, its fingerprint fields) must run one of them, as a process
    judges itself (`cell.matches`: builds N-1 and N of one configuration agree)."""
    nxt: dict[str, Any] | None = None
    if isinstance(answer.get("current_fp"), dict):
        whose = "the directory's"
        current = answer["current_fp"]
        if isinstance(answer.get("next_fp"), dict):
            nxt = answer["next_fp"]
    elif rows.current is not None:
        whose = "this cell's own row's"
        current, nxt = rows.current, rows.next
    else:
        # Nothing recorded yet: the fields the newest build reports.
        whose = ""
        current = cell.as_value(max(every, key=len))
    accepted = [v for v in (current, nxt) if v is not None]
    if not all(any(cell.matches(f, v) for v in accepted) for f in every):
        if not whose:
            raise Refused(
                "the live processes report different configurations: roll one configuration "
                "out to every process first"
            )
        raise Refused(
            f"a live process runs neither {whose} current nor its next configuration: roll it "
            "back to one of them"
        )
    return current, nxt


async def _write_rows(
    rows: _CellRows, answer: dict[str, Any], current: dict[str, Any], nxt: dict[str, Any] | None
) -> list[str]:
    """Write the `first` and mode rows that are missing and the own row where it differs
    from `current` and `nxt`, in one transaction; the rows written."""
    from server.core import pg

    is_first = bool(answer.get("is_first"))
    written = []
    async with pg.transaction() as conn:
        if rows.first is None:
            await conn.execute(
                "INSERT INTO loom.cell_first (is_first, cell) VALUES ($1, $2)",
                is_first,
                settings.cell,
            )
            written.append("first")
        if not rows.mode:
            await conn.execute(
                "INSERT INTO loom.tenancy_mode (mode) VALUES ($1)", str(answer["mode"])
            )
            written.append("mode")
        if (rows.current, rows.next) != (current, nxt):
            await conn.execute(
                "INSERT INTO loom.cell_self (current_fp, next_fp) VALUES ($1, $2) "
                "ON CONFLICT (id) DO UPDATE SET current_fp = EXCLUDED.current_fp, "
                "next_fp = EXCLUDED.next_fp, updated_at = now()",
                current,
                nxt,
            )
            written.append("own")
    return written


def _answer_problem(rows: _CellRows, answer: dict[str, Any], beats: list[_Beat]) -> str:
    """What in the router's answer this cell's rows or processes contradict."""
    is_first = answer.get("is_first")
    if not isinstance(is_first, bool) or answer.get("mode") not in cell.MODES:
        return "the router's answer names no first value or mode"
    # `prepare` refused a `first` row naming another cell: only the first value can differ.
    if rows.first is not None and rows.first[0] != is_first:
        stored = "the first cell" if rows.first[0] else "a later cell"
        named = "the first cell" if is_first else "a later cell"
        return f"this cell's `first` row says it is {stored}, and the directory says {named}"
    # The directory records the highest mode: a first cell's row below it is an attach
    # that stopped (attach's to finish), one above it is not this cell's. A later cell is
    # `enforced` from its registration.
    if rows.mode == "enforced" and answer["mode"] == "recording":
        return "this cell's mode row is `enforced`, and the directory records `recording`"
    if rows.mode == "recording" and not is_first:
        return (
            "this later cell's mode row is `recording`; a later cell is `enforced` from its "
            "registration"
        )
    if not is_first:
        unset = [b.instance_id for b in beats if b.cell != settings.cell]
        if unset:
            return (
                f"live process(es) {', '.join(unset[:5])} run without LOOM_CELL={settings.cell}: a "
                "later cell runs with it from its first boot"
            )
    return ""


async def prepare(
    ch: Any,
    args: argparse.Namespace,
    router: httpx.URL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    rows = await _read_rows()
    if not rows.deployment_id:
        raise Refused("this cell's Postgres records no deployment id: start the cell once first")
    if rows.first is not None and rows.first[1] != settings.cell:
        raise Refused(f"this cell's Postgres is cell {rows.first[1]!r}'s")
    # Refused before anything is written: a process this cell cannot take on its own.
    beats = await _read_beats(ch)
    _beat_fields(beats, rows)
    body: dict[str, Any] = {
        "host": cell.normalise_host(args.host),
        "deployment_id": rows.deployment_id,
        "credential_sha256": args.credential_sha256,
        "bucket": settings.document_s3_bucket,
        "bucket_prefix": settings.document_s3_prefix if settings.document_s3_bucket else "",
        "held": {
            "tenancy_rows": rows.tenancy_rows,
            "state_rows": _state_rows(rows),
            "prefix_objects": await _prefix_objects(),
            "processes_without_cell": sum(1 for b in beats if b.cell != settings.cell),
        },
    }
    if args.probe_url:
        body["probe_url"] = args.probe_url
    body["nonces"] = _new_nonces()
    try:
        await _write_nonces(ch, body["nonces"])
        answer = await _register(args, router, body, transport)
    finally:
        await _delete_nonces(ch, body["nonces"])
    # The processes may have changed while the probe was retried: judge the ones live now.
    beats = await _read_beats(ch)
    every = _beat_fields(beats, rows)
    if problem := _answer_problem(rows, answer, beats):
        raise Refused(problem)
    current, nxt = _own_rows(rows, answer, every)
    written = await _write_rows(rows, answer, current, nxt)
    print(
        json.dumps(
            {
                "cell": settings.cell,
                "is_first": answer["is_first"],
                "state": answer.get("state"),
                "mode": answer["mode"],
                "registered": answer.get("registered"),
                "rows_written": written,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    if problem := _args_problem(args):
        print(f"loom cell prepare: {problem}", file=sys.stderr)
        return 2
    router = cell_cli.operator_router(args)
    if isinstance(router, str):
        print(f"loom cell prepare: {router}", file=sys.stderr)
        return 2
    from server.cli._ch import run_connected

    async def _run(ch: Any) -> int:
        try:
            return await prepare(ch, args, router)
        except Refused as e:
            print(f"loom cell prepare: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001 — a store that fails names itself; a rerun resumes
            print(
                f"loom cell prepare: stopped ({type(e).__name__}: {e}); run it again",
                file=sys.stderr,
            )
            return 1

    return run_connected(_run, org_free="registers the cell; writes no org's rows")


def add_prepare_parser(csub: Any) -> None:
    p = csub.add_parser(
        "prepare", help="Register this cell in the router's directory and write its rows."
    )
    p.add_argument("--cell", required=True, help="This cell's name (LOOM_CELL)")
    p.add_argument("--host", required=True, help="The host the cell serves its orgs at")
    p.add_argument(
        "--credential-sha256",
        required=True,
        metavar="SHA256",
        help="The sha256 of the cell's router credential (`loom cell credential new`)",
    )
    p.add_argument(
        "--probe-url", default="", help="A later cell's probe listener address (not the first's)"
    )
    p.add_argument(
        "--wait",
        type=int,
        default=_WAIT_S,
        help="Seconds to retry a busy or unreachable probe listener",
    )
    p.add_argument("--router", help="The router's URL (default: LOOM_CELL_ROUTER_URL)")
    p.set_defaults(func=cmd_prepare)
