"""`loom signing-key`: the token keyring in loom.signing_keys."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from server.cli._ch import run_connected
from server.core.errors import BackendUnavailableError


def _cmd_signing_key(args: argparse.Namespace) -> int:
    from server.auth import keyring
    from server.settings import settings

    if args.signing_cmd in ("rotate", "record-root") and settings.jwt_secret_is_dev_default():
        # The servers would not unseal it: every token of the key would be refused.
        print(
            "✗ LOOM_JWT_SECRET here is the dev default; run with the servers' root secret",
            file=sys.stderr,
        )
        return 1

    async def _go(ch: Any) -> int:
        try:
            return await _signing_key_cmd(args, keyring, ch)
        except (ValueError, BackendUnavailableError) as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1

    return run_connected(_go, org_free="the cell's signing keys")


async def _servers_refresh_s(keyring: Any, ch: Any) -> float | None:
    """The slowest keyring refresh interval any serving process recorded; None
    when none did (no process serves yet, or a read-only user)."""
    try:
        slowest = await keyring.slowest_refresh_s(ch=ch)
    except Exception:  # noqa: BLE001 — the table is advisory; the caller falls back
        return None
    return None if slowest is None else float(slowest)


def _too_soon(activate_in: float, refresh_s: float, whose: str) -> bool:
    """Whether a key activating in `activate_in` s could sign before every
    process loaded it (said on stderr)."""
    if activate_in >= 2 * refresh_s:
        return False
    print(
        f"✗ --activate-in must be at least {2 * refresh_s:g} s (twice {whose} "
        "LOOM_JWT_KEYRING_REFRESH_SECONDS)",
        file=sys.stderr,
    )
    return True


async def _signing_key_cmd(args: argparse.Namespace, keyring: Any, ch: Any) -> int:
    from server.settings import settings

    if args.signing_cmd == "rotate":
        servers = await _servers_refresh_s(keyring, ch)
        if servers is not None:
            if _too_soon(args.activate_in, servers, "the servers' slowest"):
                return 1
        elif _too_soon(args.activate_in, settings.jwt_keyring_refresh_seconds, "this host's"):
            return 1
        key = await keyring.rotate(args.algorithm, activate_in_s=args.activate_in, ch=ch)
        print(f"{key.kid} ({key.algorithm}) signs from {key.activate_at:%Y-%m-%d %H:%M:%S}Z")
    elif args.signing_cmd == "retire":
        if not await keyring.retire(args.kid, ch=ch):
            print(f"no signing key {args.kid!r}", file=sys.stderr)
            return 1
        print(f"{args.kid} retired: its tokens stop verifying within a refresh interval")
    elif args.signing_cmd == "record-root":
        await keyring.record_root(ch=ch, force=True)
        print("recorded this LOOM_JWT_SECRET as the deployment's root secret")
    else:
        rows = await keyring.listing(ch=ch)
        root = next((r for r in rows if r["kid"] == keyring.ROOT_KID), None)
        print("kid  algorithm  activate_at  retired_at")
        retired = (root or {}).get("retired_at") or "-"
        signs = keyring.root_algorithm() or "seals only"
        print(f"{keyring.ROOT_KID}  {signs}  (LOOM_JWT_SECRET)  {retired}")
        for r in rows:
            if r is not root:
                print(f"{r['kid']}  {r['algorithm']}  {r['activate_at']}  {r['retired_at'] or '-'}")
    return 0


def register_signing_key_cli(sub: Any) -> None:
    p_sk = sub.add_parser(
        "signing-key",
        help="The token signing keys beside LOOM_JWT_SECRET, kept in ClickHouse.",
    )
    sk = p_sk.add_subparsers(dest="signing_cmd", required=True)
    p_rot = sk.add_parser("rotate", help="Add a key; it signs once every process has loaded it.")
    p_rot.add_argument("--algorithm", choices=("HS256", "RS256"), default="HS256")
    p_rot.add_argument(
        "--activate-in",
        type=float,
        default=300.0,
        help="seconds until it signs; at least twice the servers' "
        "LOOM_JWT_KEYRING_REFRESH_SECONDS (default 300)",
    )
    p_ret = sk.add_parser("retire", help="Stop a key (or 'default') verifying tokens.")
    p_ret.add_argument("kid")
    sk.add_parser(
        "record-root",
        help="Record this LOOM_JWT_SECRET as the deployment's root secret, after "
        "replacing it on purpose (keys sealed under the old one stay unusable).",
    )
    sk.add_parser("list", help="List the keys.")
    p_sk.set_defaults(func=_cmd_signing_key)
