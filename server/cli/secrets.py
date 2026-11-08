"""`loom secrets reseal`: after a root-secret rotation, seal every stored
credential and signing key again under the new LOOM_JWT_SECRET, so
LOOM_JWT_SECRET_PREVIOUS can be removed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from datetime import UTC, datetime
from typing import Any

from server.cli._ch import run_connected

# Every place a value `server.core.secrets_at_rest.seal` made is kept:
# (table, column, JSON path inside the column (() = the column itself), purpose,
# extra filter). A ReplacingMergeTree(version) each, so a row written at a
# higher version replaces the one read. Kid `default` holds the root's own
# record, which `loom signing-key record-root` writes.
SEALED: tuple[tuple[str, str, tuple[str, ...], str, str], ...] = (
    ("loom.signing_keys", "sealed_key", (), "signing_key", "kid != 'default'"),
    ("loom.code_repos", "github_token_enc", (), "code_repos.token", ""),
    ("loom.qa_sources", "password_enc", (), "qa_sources.password", ""),
    ("loom.knowledge_sources", "credential_enc", (), "knowledge_sources.credential", ""),
    ("loom.provider_configuration", "envelope", (), "model-provider-configuration", ""),
    (
        "loom.provider_configuration",
        "progress",
        ("recipe", "views"),
        "provider-rebuild-views",
        "",
    ),
    ("loom.analysis_secrets", "sealed", (), "analysis.secrets", ""),
    ("loom.assistant_service", "sealed", (), "assistant.service", ""),
    # A live session's identity-provider refresh token. Keeper's sealed copies
    # (a code, a device grant, an approval or link in flight) are not rewritten
    # here, see `in_flight`; a `family:<id>` key holds only "<generation>:<rid>".
    (
        "loom.oauth_state",
        "upstream_refresh",
        (),
        "oidc refresh token",
        "record_type = 'session_refresh' AND expires_at > toUnixTimestamp(now())",
    ),
)
# Each table's key, to name a row whose value opens with neither root.
_KEYS = {
    "loom.signing_keys": ("kid",),
    "loom.code_repos": ("org", "namespace"),
    "loom.qa_sources": ("org", "name"),
    "loom.knowledge_sources": ("org", "namespace", "name"),
    "loom.provider_configuration": ("config_id",),
    "loom.oauth_state": ("record_type", "id"),
    "loom.analysis_secrets": ("id",),
    "loom.assistant_service": ("id",),
}


def _analysis_lease(ch: Any) -> Any:
    from server.insights.app.analysis import lease

    return lease(ch)


def _assistant_lease(ch: Any) -> Any:
    from server.assistant.app.service import lease

    return lease(ch)


# The rows a boot writes under a cluster lease, which the reseal takes too.
_LEASED = {"loom.analysis_secrets": _analysis_lease, "loom.assistant_service": _assistant_lease}
# A rebuild in these states rewrites its configuration row from one it read
# earlier, which would put back what the reseal replaced.
_REBUILDING = ("queued", "rebuilding", "validating")


class NotTheRoot(RuntimeError):
    pass


def _resealed_value(raw: str, path: tuple[str, ...], purpose: str) -> tuple[str | None, bool]:
    """The column value sealed again under the root secret (None: nothing to
    rewrite), and whether its sealed value opens with either root."""
    from server.core.secrets_at_rest import resealed, unseal

    if not path:
        return resealed(purpose, raw), bool(unseal(purpose, raw, warn=False))
    try:
        doc = json.loads(raw or "{}")
    except ValueError:
        return None, True
    node = doc
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict) or not isinstance(node.get(path[-1]), str) or not node[path[-1]]:
        return None, True
    sealed = node[path[-1]]
    new = resealed(purpose, sealed)
    if new is None:
        return None, bool(unseal(purpose, sealed, warn=False))
    node[path[-1]] = new
    return json.dumps(doc, sort_keys=True), True


async def _check_previous(ch: Any) -> bool:
    """LOOM_JWT_SECRET_PREVIOUS must be the root this deployment recorded (before
    the publish) or LOOM_JWT_SECRET (after it): a mistyped one would reseal
    nothing and let the old root be dropped with the values only it opens.
    False when no root is recorded, so nothing could be checked."""
    from server.auth.keyring import records_root
    from server.settings import settings

    row = await ch.query_one("SELECT sealed_key FROM loom.signing_keys FINAL WHERE kid = 'default'")
    recorded = str(row["sealed_key"]) if row else ""
    if recorded and not any(
        records_root(recorded, str(s)) for s in (settings.jwt_secret, settings.jwt_secret_previous)
    ):
        raise NotTheRoot(
            "LOOM_JWT_SECRET_PREVIOUS is not the root this deployment recorded; nothing was written"
        )
    return bool(recorded)


async def reseal_all(
    ch: Any, *, dry_run: bool
) -> tuple[dict[str, int], dict[str, list[str]], bool]:
    """Per table and column: how many values opened only with the previous root
    (and were written again, unless `dry_run`), and the keys of the rows whose
    value opens with neither (lost before this rotation: re-enter them). The
    last item says whether the previous root was checked against the recorded
    one; without that, a value neither root opens may mean a wrong previous root."""
    checked = await _check_previous(ch)
    busy = await ch.query_all(
        "SELECT config_id FROM loom.provider_configuration FINAL WHERE state IN {s:Array(String)}",
        params={"s": list(_REBUILDING)},
    )
    if busy:
        raise RuntimeError("a provider rebuild is under way; reseal once it has finished")
    counts: dict[str, int] = {}
    lost: dict[str, list[str]] = {}
    for table, column, path, purpose, where in SEALED:
        async with contextlib.AsyncExitStack() as held:
            if table in _LEASED:
                # One writer of the row at a time, reading the newest copy.
                from server.core.ch import own_write_reads

                await held.enter_async_context(_LEASED[table](ch))
                held.enter_context(own_write_reads())
            rows = await ch.query_all(
                f"SELECT * FROM {table} FINAL WHERE {column} != ''"
                + (f" AND {where}" if where else "")
            )
            out = []
            unopened: list[str] = []
            for row in rows:
                new, opens = _resealed_value(str(row[column]), path, purpose)
                if not opens:
                    unopened.append("/".join(str(row[k]) for k in _KEYS[table]))
                if new is not None:
                    # The lowest version above the one read: an edit written
                    # meanwhile (at the server's clock) stays the newer row.
                    out.append({**row, column: new, "version": int(row["version"]) + 1})
            counts[f"{table}.{column}"] = len(out)
            if unopened:
                lost[f"{table}.{column}"] = unopened
            if out and not dry_run:
                await ch.insert(table, out, settings={"async_insert": 0})
    return counts, lost, checked


async def in_flight(ch: Any) -> tuple[int, int]:
    """Sign-ins in flight in Keeper whose IdP refresh token opens only with the
    previous root: how many, and when the last expires (epoch seconds). Not
    resealed: the request holding the value it read would lose its own
    compare-and-set to the rewrite. They live minutes."""
    from server.auth.oauth_store import UPSTREAM_PURPOSE
    from server.core.secrets_at_rest import resealed

    rows = await ch.query_all(
        "SELECT value, expires_at FROM loom.oauth_signin_state WHERE expires_at > toUInt32(now())"
    )
    n, last = 0, 0
    for row in rows:
        try:
            doc = json.loads(row["value"])
        except ValueError:
            continue  # a session's "<generation>:<rid>"
        if not isinstance(doc, dict) or doc.get("spent"):
            continue  # a spent code or grant has no sign-in left to finish
        # A code's or a device grant's at the top, an approval's or a link's in its data.
        data = doc["data"] if isinstance(doc.get("data"), dict) else {}
        sealed = doc.get("upstream_refresh") or data.get("upstream_refresh")
        if isinstance(sealed, str) and resealed(UPSTREAM_PURPOSE, sealed) is not None:
            n, last = n + 1, max(last, int(row["expires_at"]))
    return n, last


def _cmd_reseal(args: argparse.Namespace) -> int:
    from server.settings import settings

    if not settings.jwt_secret_previous:
        print(
            "✗ set LOOM_JWT_SECRET_PREVIOUS to the root secret that was replaced", file=sys.stderr
        )
        return 2

    async def _go(ch: Any) -> int:
        if not (args.dry_run or args.check):
            # A process on the old root seals under it until its next heartbeat
            # finds it drifted (then it refuses every write): let them all see it.
            await asyncio.sleep(2 * float(settings.instance_heartbeat_seconds))
        dry = args.dry_run or args.check
        try:
            counts, lost, checked = await reseal_all(ch, dry_run=dry)
        except Exception as e:  # noqa: BLE001 — reported with a failing exit
            print(f"✗ reseal failed: {e}", file=sys.stderr)
            return 1
        try:
            pending, until = await in_flight(ch)
        except Exception as e:  # noqa: BLE001 — only the check needs it; a reseal goes on
            print(f"! cannot read the sign-ins in flight: {e}", file=sys.stderr)
            if args.check:
                return 1
            pending, until = 0, 0
        for where, n in counts.items():
            print(f"  {where}: {n} {'to reseal' if dry else 'resealed'}")
        if pending:
            last = datetime.fromtimestamp(until, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(
                f"  loom.oauth_signin_state: {pending} sign-in{'s' * (pending > 1)} in flight "
                f"under the previous root, the last expiring at {last}"
            )
        for where, keys in lost.items():
            print(f"! {where}: opens with neither root, enter it again: {', '.join(keys)}")
        if lost and not checked:
            print("✗ no recorded root to check LOOM_JWT_SECRET_PREVIOUS against", file=sys.stderr)
            return 1
        if args.check and (any(counts.values()) or pending):
            print("✗ values still open only with LOOM_JWT_SECRET_PREVIOUS", file=sys.stderr)
            return 1
        print("✓ dry run, nothing written" if dry else "✓ resealed under LOOM_JWT_SECRET")
        return 0

    return run_connected(_go)


def register_secrets_cli(sub: Any) -> None:
    p = sub.add_parser("secrets", help="Credentials and signing keys stored sealed in ClickHouse.")
    ssub = p.add_subparsers(dest="secrets_cmd", required=True)
    rs = ssub.add_parser(
        "reseal",
        help="Seal again under LOOM_JWT_SECRET what only LOOM_JWT_SECRET_PREVIOUS opens.",
    )
    rs.add_argument("--dry-run", action="store_true", help="Count, write nothing")
    rs.add_argument(
        "--check",
        action="store_true",
        help="Write nothing; exit 1 while a value, or a sign-in in flight, opens only with "
        "LOOM_JWT_SECRET_PREVIOUS",
    )
    rs.set_defaults(func=_cmd_reseal)
