"""`loom cell credential | limit | which`: the operator's commands that touch the router's
directory alone (docs/architecture/cells.md, "Operations"). None of them connects to a
cell's stores, so they run while a cell is down. `loom cell prepare`, which does, is
`server/cli/cell_prepare.py`.

- `credential new` makes a router credential offline and prints it with its SHA-256;
  `credential --cell <cell> --next <sha256> | --finish | --revert` rotates a cell's.
- `limit --cell <cell> [--closed | --open] [--placements-max (<n> | none)]` closes,
  reopens or bounds a cell to new orgs.
- `which --org <org>` prints the cell and host the router's lookup answers for an org
  (the first cell's for an org with no placement, which it cannot tell apart).

`credential` (except `new`) and `limit` call the router's operator API with
`LOOM_ROUTER_OPERATOR_TOKEN`; `which` asks the public lookup. The router is `--router`,
else `LOOM_CELL_ROUTER_URL`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from typing import Any

import httpx

from server.auth.public_url import is_loopback_host
from server.core.cell import NAME, PLACEMENTS_MAX, SHA256
from server.router.directory import OPERATOR_CHANGE_MAX_S, STATEMENT_TIMEOUT_S
from server.settings import settings

# An operator call's read waits past the router's longest operator change. A lookup makes
# up to two directory reads (the org, then the first cell), each a slot-and-connection wait
# and a statement. A connect fails fast.
_OPERATOR_TIMEOUT = httpx.Timeout(  # not a setting: derived
    OPERATOR_CHANGE_MAX_S + 10.0, connect=5.0
)
_LOOKUP_TIMEOUT = httpx.Timeout(  # not a setting: derived
    2 * (2 * STATEMENT_TIMEOUT_S + 1) + 10.0, connect=5.0
)
# The statuses each refuses with, always with an `error` string.
OPERATOR_REFUSALS = frozenset({400, 401, 404, 409, 503})
_LOOKUP_REFUSALS = frozenset({400, 429, 503})


def new_credential(prefix: str = "lcr_") -> tuple[str, str]:
    """A credential (`lcr_` for a cell's router credential, `lco_` for the router's
    operator credential, then 128 lowercase hex: 512 bits) and its SHA-256."""
    credential = prefix + secrets.token_hex(64)
    return credential, hashlib.sha256(credential.encode()).hexdigest()


def parse_router(args: argparse.Namespace) -> httpx.URL | str:
    """The router's URL, or why it is not one."""
    url = args.router or settings.cell_router_url
    if not url:
        return "no router: pass --router or set LOOM_CELL_ROUTER_URL"
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        parsed = None
    # No path, query or fragment: the router answers its API at its root and forwards
    # anything else to a cell, credential included. No user: httpx would send it as a
    # second credential.
    if (
        parsed is None
        or parsed.scheme not in ("http", "https")
        or not parsed.host
        or parsed.userinfo
        or parsed.raw_path not in (b"", b"/")
        or "?" in url
        or "#" in url
    ):
        return (
            "the router URL must be http or https with a host, and no path, query, fragment or user"
        )
    return parsed


def at(router: httpx.URL, path: str) -> str:
    """`path` at the router's root, set as the path alone."""
    return str(router.copy_with(raw_path=path.encode()))


def operator_router(args: argparse.Namespace) -> httpx.URL | str:
    """The router the operator credential may be sent to, or why it may not."""
    if not settings.router_operator_token:
        return "set LOOM_ROUTER_OPERATOR_TOKEN, the router's operator credential"
    router = parse_router(args)
    if isinstance(router, str):
        return router
    if router.scheme != "https" and not (router.scheme == "http" and is_loopback_host(router.host)):
        return "the operator credential goes only to an https router (http only on loopback)"
    return router


def _operator_call(args: argparse.Namespace, path: str, body: dict[str, Any], retry: str) -> int:
    """The outcome is known only when the connection never opened, or the router itself
    answered, other than with the code `BACKEND_UNAVAILABLE`; after anything else (a
    timeout, a dropped connection, a gateway's page) the change may have been made, and
    `retry` says what a retry then shows."""
    if not NAME.fullmatch(args.cell or ""):
        print(f"loom cell: {args.cell!r} is no cell name (1 to 32 of a-z, 0-9, -)", file=sys.stderr)
        return 2
    router = operator_router(args)
    if isinstance(router, str):
        print(f"loom cell: {router}", file=sys.stderr)
        return 2
    token = settings.router_operator_token
    url = at(router, f"/cells/v1/operator/cells/{args.cell}/{path}")
    try:
        # No proxy from the environment: plain http on loopback must not reach one.
        r = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_OPERATOR_TIMEOUT,
            trust_env=router.scheme == "https",
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as e:
        print(f"loom cell: the router could not be reached: {type(e).__name__}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"loom cell: no answer from the router: {type(e).__name__}", file=sys.stderr)
    else:
        answer = router_answer(r, {"cell": args.cell}, OPERATOR_REFUSALS)
        if answer is None:
            print(f"loom cell: {not_the_router(r)}", file=sys.stderr)
        elif answer.get("code") == "BACKEND_UNAVAILABLE":
            print(f"loom cell: the directory did not answer: {answer['error']}", file=sys.stderr)
        else:
            return _said(r, answer)
    print(f"loom cell: the change may have been made: {retry}", file=sys.stderr)
    return 1


def router_answer(
    r: httpx.Response, names: dict[str, str], refusals: frozenset[int]
) -> dict[str, Any] | None:
    """The router's own answer: 200 with a JSON object naming what was asked (`names`),
    or one of its refusals with an `error` string. Anything else (a gateway's 502 or 504,
    JSON or not, another service's page) is None."""
    try:
        answer = r.json()
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    if r.status_code == 200:
        return answer if all(answer.get(k) == v for k, v in names.items()) else None
    if r.status_code not in refusals or not isinstance(answer.get("error"), str):
        return None
    # Every 503 the lookup and the operator API give carries a code; one without is a
    # proxy's.
    if r.status_code == 503 and not isinstance(answer.get("code"), str):
        return None
    return answer


def not_the_router(r: httpx.Response) -> str:
    return f"{r.url} did not answer as the router does ({r.status_code}): {r.text[:200]!r}"


def _said(r: httpx.Response, answer: dict[str, Any]) -> int:
    """Print the router's answer; the exit code."""
    if r.status_code != 200:
        print(
            f"loom cell: the router refused ({r.status_code}): {answer['error']}", file=sys.stderr
        )
        return 1
    print(json.dumps(answer, indent=2, sort_keys=True))
    return 0


def _cmd_credential(args: argparse.Namespace) -> int:
    if args.action == "new":
        if args.cell or args.next or args.finish or args.revert or args.router:
            print("loom cell credential new takes no option but --operator", file=sys.stderr)
            return 2
        credential, sha = new_credential("lco_" if args.operator else "lcr_")
        print(f"credential: {credential}\nsha256:     {sha}")
        where = (
            "the operator's LOOM_ROUTER_OPERATOR_TOKEN, and the sha256 in the router's "
            "LOOM_ROUTER_OPERATOR_SHA256"
            if args.operator
            else "the cell's LOOM_CELL_ROUTER_TOKEN, and give `--next` the sha256"
        )
        print(f"Put the credential in {where}; it is shown once.", file=sys.stderr)
        return 0
    if args.operator:
        print("loom cell credential: --operator goes with `new`", file=sys.stderr)
        return 2
    steps = [
        s for s, on in (("next", args.next), ("finish", args.finish), ("revert", args.revert)) if on
    ]
    if not args.cell or len(steps) != 1:
        print(
            "loom cell credential: `new`, or --cell <cell> with one of --next <sha256>, "
            "--finish, --revert",
            file=sys.stderr,
        )
        return 2
    body: dict[str, Any] = {"step": steps[0]}
    if args.next:
        if not SHA256.fullmatch(args.next):
            print(
                "loom cell credential --next takes the credential's sha256 (64 lowercase hex), "
                "never the credential: `loom cell credential new` prints both",
                file=sys.stderr,
            )
            return 2
        body["sha256"] = args.next
    return _operator_call(
        args,
        "credential",
        body,
        "a retry refused as already the cell's next, or with none pending, means it was",
    )


def _cmd_limit(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.closed or args.open:
        body["closed"] = bool(args.closed)
    if args.placements_max is not None:
        if args.placements_max == "none":
            body["placements_max"] = None
        else:
            v = args.placements_max
            if not (v.isascii() and v.isdigit() and len(v) <= 19 and int(v) <= PLACEMENTS_MAX):
                print(
                    "loom cell limit: --placements-max is a whole number below 2**63, or none",
                    file=sys.stderr,
                )
                return 2
            body["placements_max"] = int(v)
    if not body:
        print("loom cell limit: pass --closed, --open or --placements-max", file=sys.stderr)
        return 2
    return _operator_call(args, "limit", body, "run it again, a limit is safe to repeat")


def _cmd_which(args: argparse.Namespace) -> int:
    router = parse_router(args)
    if isinstance(router, str):
        print(f"loom cell: {router}", file=sys.stderr)
        return 2
    try:
        r = httpx.get(
            at(router, "/cells/lookup"), params={"org": args.org}, timeout=_LOOKUP_TIMEOUT
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as e:
        print(f"loom cell: the router could not be reached: {type(e).__name__}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"loom cell: no answer from the router: {type(e).__name__}", file=sys.stderr)
        return 1
    answer = router_answer(r, {"org": args.org}, _LOOKUP_REFUSALS)
    if answer is None:
        print(f"loom cell: {not_the_router(r)}", file=sys.stderr)
        return 1
    if answer.get("code") == "BACKEND_UNAVAILABLE":
        print(
            f"loom cell: the directory did not answer, try again: {answer['error']}",
            file=sys.stderr,
        )
        return 1
    return _said(r, answer)


def _add_cell_parser(sub: Any) -> None:
    p = sub.add_parser("cell", help="Operate a deployment's cells through the router's directory.")
    csub = p.add_subparsers(dest="cell_cmd", required=True)

    cred = csub.add_parser("credential", help="Make a router credential, or rotate a cell's.")
    cred.add_argument("action", nargs="?", choices=["new"], help="new: make one offline")
    cred.add_argument("--cell", help="The cell whose credential to rotate")
    cred.add_argument("--next", metavar="SHA256", help="Record a new credential's sha256 as next")
    cred.add_argument("--finish", action="store_true", help="Make the next credential current")
    cred.add_argument("--revert", action="store_true", help="Drop the pending next credential")
    cred.add_argument(
        "--operator", action="store_true", help="With new: make the router's operator credential"
    )
    cred.add_argument("--router", help="The router's URL (default: LOOM_CELL_ROUTER_URL)")
    cred.set_defaults(func=_cmd_credential)

    limit = csub.add_parser("limit", help="Close, reopen or bound a cell to new orgs.")
    limit.add_argument("--cell", required=True, help="The cell to limit")
    state = limit.add_mutually_exclusive_group()
    state.add_argument("--closed", action="store_true", help="Refuse its new orgs")
    state.add_argument("--open", action="store_true", help="Take new orgs again")
    limit.add_argument(
        "--placements-max", metavar="N|none", help="Its quota of orgs (none: no quota)"
    )
    limit.add_argument("--router", help="The router's URL (default: LOOM_CELL_ROUTER_URL)")
    limit.set_defaults(func=_cmd_limit)

    which = csub.add_parser(
        "which", help="Print the lookup's cell and host for an org (the first cell if not placed)."
    )
    which.add_argument("--org", required=True, help="The org")
    which.add_argument("--router", help="The router's URL (default: LOOM_CELL_ROUTER_URL)")
    which.set_defaults(func=_cmd_which)

    from server.cli.cell_prepare import add_prepare_parser

    add_prepare_parser(csub)
