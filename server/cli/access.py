"""Access commands: `loom mint-token` (issue a JWT locally), `loom
revoke-token` (write loom.revoked_tokens) and `loom claim-release` (release a
self-signup namespace claim).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

from server.cli._ch import run_connected
from server.cli._client import token_path


def _cmd_claim_release(args: argparse.Namespace) -> int:
    """Operator recovery: release a self-signup claim. Use when a user lost
    their token or when cleaning up a stale signup.

    Revokes every grant, clears the owner and deactivates the namespace — the
    same version-row write as `POST /v1/admin/claim_release`. The registry row
    stays, so the name stays reserved: its memories are still there, and a
    fresh signup claiming the name would become admin over them. Erasing the
    namespace (`erasure.request` scope `namespace`) removes the data; the name
    stays reserved even then.
    """

    from server.identity.app.claim_release import release_claim

    async def _lookup_owner(ch: Any) -> str | None:
        from server.identity.store import namespaces as namespaces_repo

        row = await namespaces_repo.fetch_row(args.org, args.namespace, ch=ch)
        if row is None:
            return None
        return str(row.get("owner_email") or "") or "(unknown)"

    owner = run_connected(_lookup_owner)
    if owner is None:
        print(
            f"no claim found for {args.org}/{args.namespace}",
            file=sys.stderr,
        )
        return 1
    if not args.yes:
        print(
            f"This will release the claim on {args.org}/{args.namespace}"
            f" (currently owned by {owner!r})."
        )
        print(
            "Grants are revoked and the namespace deactivated. Its memories are "
            "kept, and the name stays reserved: nobody can claim it again."
        )
        try:
            resp = input("Continue? [y/N] ").strip().lower()
        except EOFError:  # no terminal to answer (a pipe, a cron): the answer is no
            resp = ""
        if resp not in ("y", "yes"):
            print("aborted")
            return 1

    async def _go(ch: Any) -> int:
        out = await release_claim(
            org=args.org,
            namespace=args.namespace,
            reason="",
            actor_subject="loom-cli",
            ch=ch,
        )
        if out is None:
            print(f"no claim found for {args.org}/{args.namespace}", file=sys.stderr)
            return 1
        print(
            f"released claim on {args.org}/{args.namespace}"
            f" ({out.grants_revoked} grants revoked; the name stays reserved)"
        )
        return 0

    return run_connected(_go)


def _normalize_namespace_args(values: list[str] | None) -> list[str]:
    """`--namespaces a b` and `--namespaces a,b` must mean the same thing.

    argparse's nargs="+" is space-separated, so a comma-joined list minted ONE
    namespace literally named "a,b" — a token that 403s on every real
    namespace and reads as "membership is broken" to the caller.
    Split on commas, drop empties, keep order, dedupe. A name that still
    carries whitespace or a comma is rejected by the caller.
    """
    out: list[str] = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


_NS_FORBIDDEN = set(" \t\r\n,")


def _invalid_namespace_names(names: list[str]) -> list[str]:
    return [n for n in names if any(ch in _NS_FORBIDDEN for ch in n)]


def _cmd_mint_token(args: argparse.Namespace) -> int:
    from server.auth.authz import GRANT_ROLES
    from server.auth.jwt import mint_token, new_jti

    ttl_seconds = (args.ttl_days * 86400) if args.ttl_days else None
    # Every minted token carries a jti; generated here so
    # the summary can name it beside (or instead of) the token — it is the
    # handle `loom revoke-token --jti` retires and the ledgers' token_id.
    jti = new_jti()

    # Scoped mode: --namespace-role NS=ROLE (repeatable). Arg order = read
    # priority. Emits an ns_roles claim → a scoped token (restricted to these
    # namespaces, ordered, authoritative per-namespace roles). This is a local
    # operator mint with the raw secret, so it is NOT capped against grants —
    # the /v1/me/service_accounts.create endpoint enforces that cap.
    namespaces = _normalize_namespace_args(args.namespaces)
    bad = _invalid_namespace_names(namespaces)
    if bad:
        print(f"  ✗ invalid namespace name(s): {bad!r}", file=sys.stderr)
        return 2
    if args.namespace_role:
        if namespaces:
            print(
                "  ✗ pass either --namespaces or --namespace-role, not both",
                file=sys.stderr,
            )
            return 2
        ordered_ns: list[str] = []
        ns_roles: dict[str, str] = {}
        for item in args.namespace_role:
            ns, sep, role = item.partition("=")
            ns, role = ns.strip(), role.strip().lower()
            if not sep or not ns or role not in GRANT_ROLES:
                print(
                    f"  ✗ --namespace-role must be NS=viewer|writer|admin, got {item!r}",
                    file=sys.stderr,
                )
                return 2
            if ns not in ns_roles:
                ordered_ns.append(ns)
            ns_roles[ns] = role
        token = mint_token(
            subject=args.subject,
            org=args.org,
            namespaces=ordered_ns,
            roles=[],
            ns_roles=ns_roles,
            ttl_seconds=ttl_seconds,
            is_service_account=args.service_account,
            read_scope=args.read_scope,
            read_user_id=args.read_user_id,
            traffic_class=args.traffic_class,
            consumer=args.consumer,
            jti=jti,
        )
        summary = {
            "token": token,
            "jti": jti,
            "subject": args.subject,
            "org": args.org,
            "namespaces": ordered_ns,
            "ns_roles": ns_roles,
            "ttl_days": args.ttl_days,
            "is_service_account": args.service_account,
            "read_scope": args.read_scope or "namespace",
            "read_user_id": args.read_user_id or "",
            "traffic_class": args.traffic_class or "",
            "consumer": args.consumer or "",
        }
    else:
        if not namespaces:
            print(
                "  ✗ one of --namespaces or --namespace-role is required",
                file=sys.stderr,
            )
            return 2
        token = mint_token(
            subject=args.subject,
            org=args.org,
            namespaces=namespaces,
            roles=args.roles,
            ttl_seconds=ttl_seconds,
            is_service_account=args.service_account,
            read_scope=args.read_scope,
            read_user_id=args.read_user_id,
            traffic_class=args.traffic_class,
            consumer=args.consumer,
            jti=jti,
        )
        summary = {
            "token": token,
            "jti": jti,
            "subject": args.subject,
            "org": args.org,
            "namespaces": namespaces,
            "roles": args.roles,
            "ttl_days": args.ttl_days,
            "is_service_account": args.service_account,
            "read_scope": args.read_scope or "namespace",
            "read_user_id": args.read_user_id or "",
            "traffic_class": args.traffic_class or "",
            "consumer": args.consumer or "",
        }

    return _emit_minted_token(args, token, summary)


def _emit_minted_token(args: argparse.Namespace, token: str, summary: dict[str, Any]) -> int:
    """Print a minted token the way the caller asked: bare on stdout (for
    `$( )`), a JSON summary with `--json`, or — with `--save [PATH]`
    — write it to the file the help strings name and print
    only the path, so the secret never lands in a terminal or a shell
    history."""
    # A token signed with the checked-in dev secret is not a
    # secret. Say so once, on stderr, so stdout stays a bare token for `$( )`.
    from server.cli.doctor import dev_jwt_secret_warning
    from server.cli.init import write_token

    if warning := dev_jwt_secret_warning():
        print(f"  ! WARNING: {warning}", file=sys.stderr)
    if args.save is not None:
        path = Path(args.save).expanduser() if args.save else token_path()
        try:
            path = write_token(token, path)
        except OSError as e:
            print(f"  ✗ could not write {path}: {e}", file=sys.stderr)
            return 1
        summary = {k: v for k, v in summary.items() if k != "token"}
        summary["token_path"] = str(path)
        if args.json:
            print(json.dumps(summary))
        else:
            print(str(path))
        if args.ttl_days is None:
            print(
                "  i no --ttl-days: this token never expires; retire it with "
                f"`loom revoke-token --subject {args.subject}` (every token of this "
                f"subject) or `--jti {summary.get('jti', '')}` (this token only).",
                file=sys.stderr,
            )
        return 0
    if args.json:
        print(json.dumps(summary))
    else:
        print(token)
    return 0


def _parse_expires_at(value: str | None) -> Any:
    """`--expires-at` as a naive UTC datetime, or None for "never"."""
    if not value:
        return None
    from datetime import datetime

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _cmd_revoke_token(args: argparse.Namespace) -> int:
    """Revoke one token through `server.auth.revocation`, the
    same path the `/v1/me/service_accounts.revoke` handler and the refresh
    worker use — never a hand INSERT. `verify_token` checks a bearer's `jti`
    and its `sub` against the one revoked column, so `--jti` retires an
    IdP-minted JWT or a `loom mint-token` token (each carries one; mint-token
    prints it) and `--subject` retires every token minted for that
    principal. Other replicas see the row on their next refresh tick.
    `--subject S --issued-before T` retires only the tokens
    minted for S before T, so a token re-minted after the leak keeps working.
    `--list` prints the unexpired revocations."""
    import getpass

    from server.auth import revocation
    from server.observability import audit
    from server.settings import settings

    try:
        expires_at = _parse_expires_at(args.expires_at)
        issued_before = _parse_expires_at(args.issued_before)
    except ValueError as e:
        print(
            f"  ✗ --expires-at / --issued-before must be ISO-8601 (e.g. 2027-01-31T00:00:00Z): {e}",
            file=sys.stderr,
        )
        return 2
    if issued_before is not None and not args.subject:
        print("  ✗ --issued-before narrows a --subject revocation; pass --subject", file=sys.stderr)
        return 2

    async def _run(ch: Any) -> int:
        if args.list:
            return _print_revocations(await revocation.list_active(), as_json=args.json)
        token_id = args.jti or args.subject
        by = f"cli:{getpass.getuser()}"
        row = await revocation.revoke(
            token_id,
            expires_at=expires_at,
            reason=args.reason or "",
            revoked_by=by,
            issued_before=issued_before,
        )
        kind = "jti" if args.jti else "subject"
        # A CLI revoke has no request ledger row; this is its only durable
        # record. Identifiers only, never token material. Revocations span
        # every org, hence the '-' scope.
        await audit.emit(
            "token.revoked",
            org="-",
            namespace="-",
            actor=by,
            subject={
                "matched_claim": kind,
                "token_id": token_id,
                "issued_before": row["issued_before"],
                "expires_at": row["expires_at"],
                "reason": row["reason"],
            },
        )
        if args.json:
            print(json.dumps({**row, "matched_claim": kind}, default=str))
        else:
            print(f"revoked {kind} {token_id!r}", end="")
            if kind == "subject" and issued_before is not None:
                print(
                    f" for tokens issued before {issued_before.isoformat()}Z "
                    "(in every org; a token minted after that stays valid)",
                    end="",
                )
            elif kind == "subject":
                print(" (every token whose sub is this principal, in every org)", end="")
            print(
                f"; other replicas pick it up within "
                f"{settings.jwt_revocation_cache_ttl_seconds:g} s"
            )
        return 0

    try:
        return run_connected(_run)
    except Exception as e:  # noqa: BLE001 — a revocation that did not land must say so
        print(f"  ✗ revocation not recorded: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _print_revocations(rows: list[dict[str, Any]], *, as_json: bool) -> int:
    from server.auth.revocation import CUTOFF_PREFIX

    if as_json:
        print(json.dumps(rows, default=str))
        return 0
    if not rows:
        print("no active revocations")
        return 0
    print(
        f"{'jti / sub':<40} {'revoked_at':<20} {'expires_at':<20} "
        f"{'issued_before':<20} revoked_by  reason"
    )
    for r in rows:
        before = r.get("issued_before")
        key = str(r.get("jti", ""))
        if key.startswith(CUTOFF_PREFIX):  # an operator cutoff, shown without its NUL
            key = "cutoff→" + key.removeprefix(CUTOFF_PREFIX)
        print(
            f"{key:<40} {str(r.get('revoked_at', ''))[:19]:<20} "
            f"{str(r.get('expires_at', ''))[:19]:<20} "
            f"{(str(before)[:19] if before else '-'):<20} {r.get('revoked_by', '')}  "
            f"{r.get('reason', '')}"
        )
    return 0


def _add_revoke_token_parser(sub: Any) -> None:
    """`loom revoke-token`; a helper so `main` does not grow."""
    p_revoke = sub.add_parser(
        "revoke-token",
        help="Revoke one token by jti or subject (writes loom.revoked_tokens "
        "through the revocation module; needs the configured ClickHouse).",
    )
    which = p_revoke.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--jti",
        default=None,
        help="The `jti` claim: retires exactly one token. Every token from "
        "`loom mint-token` (printed with --json / --save) and the admin endpoint "
        "carries one; older tokens may not — use --subject.",
    )
    which.add_argument(
        "--subject",
        default=None,
        help="The `sub` claim: retires EVERY token minted for this principal, in "
        "every org (verification checks sub against the revoked set); narrow it "
        "with --issued-before.",
    )
    which.add_argument("--list", action="store_true", help="Print the unexpired revocations.")
    p_revoke.add_argument(
        "--issued-before",
        default=None,
        metavar="ISO-8601",
        help="With --subject: revoke only the tokens issued (`iat`) before this "
        "instant, so a token re-minted after a leak keeps working.",
    )
    p_revoke.add_argument(
        "--reason", default=None, help="Free text stored on the row (audit trail)."
    )
    p_revoke.add_argument(
        "--expires-at",
        default=None,
        metavar="ISO8601",
        help="When the revocation may lapse — the token's own exp. Omit for a "
        "token without exp: the row is kept for 100 years.",
    )
    p_revoke.add_argument("--json", action="store_true", help="Machine-readable output.")
    p_revoke.set_defaults(func=_cmd_revoke_token)


def _add_mint_token_parser(sub: Any) -> None:
    """The `mint-token` subcommand."""
    p_mint = sub.add_parser("mint-token", help="Mint a JWT locally (requires LOOM_JWT_SECRET).")
    p_mint.add_argument("--subject", required=True)
    p_mint.add_argument("--org", required=True)
    p_mint.add_argument(
        "--namespaces",
        nargs="+",
        default=None,
        help="Namespace(s) the token covers, space- or comma-separated. Use '*' "
        "for wildcard. Mutually exclusive with --namespace-role.",
    )
    p_mint.add_argument(
        "--roles", nargs="+", default=["writer"], choices=["viewer", "writer", "admin"]
    )
    p_mint.add_argument(
        "--namespace-role",
        action="append",
        default=None,
        metavar="NS=ROLE",
        help="Scoped per-namespace role, repeatable; ARG ORDER = read priority "
        "(first = searched first). e.g. --namespace-role priv=admin "
        "--namespace-role pub=viewer. Emits an ns_roles claim (a scoped token: "
        "restricted to these namespaces, ordered, per-namespace roles).",
    )
    p_mint.add_argument(
        "--ttl-days",
        type=int,
        default=None,
        help="Token lifetime in days. Omit for no expiry (the default, kept for "
        "dev boxes and `$( )` scripts). Recommended for anything that leaves "
        "your machine: --ttl-days 30 and re-mint on a schedule — a token "
        "without an exp can only be retired with `loom revoke-token`.",
    )
    p_mint.add_argument(
        "--save",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Write the token to PATH (default ~/.loom/token — the file the "
        "SDK, `loom import-*`, `loom okf` and the Claude Code hooks read) "
        "with mode 0600, creating ~/.loom as 0700, and print the path "
        "instead of the token.",
    )
    p_mint.add_argument(
        "--read-scope",
        choices=["namespace", "own_user", "own_session"],
        default=None,
        help="Widest read this token may make inside its namespaces: namespace "
        "(every row, the default), own_user (only rows of the user_id each "
        "request names), own_session (only rows of the session_id each request "
        "names). Mint own_user for an agent that serves many end users through "
        "one token.",
    )
    p_mint.add_argument(
        "--read-user-id",
        default=None,
        metavar="USER_ID",
        help="Pin the token to one end user: reads are filtered to this user_id "
        "and a request naming another user is refused.",
    )
    p_mint.add_argument(
        "--traffic-class",
        choices=["production", "interactive", "bench"],
        default=None,
        help="Traffic class every request under this token runs as: "
        "its own rate buckets, ClickHouse priority, LLM-tier policy and "
        "cost-ledger column. Omit to let the namespace row decide (kind = bench "
        "→ bench, a registered namespace → production, unknown → interactive).",
    )
    p_mint.add_argument(
        "--consumer",
        default=None,
        metavar="NAME",
        help="Bind the consumer tag to this credential: every "
        "request under the token is attributed to NAME whatever its body says, "
        "and its ledger rows read consumer_source=claimed. Omit to let the "
        "request's own `consumer` field (self-reported) or the subject class decide.",
    )
    p_mint.add_argument(
        "--service-account",
        action="store_true",
        default=True,
        help="Mark token as a service account (default).",
    )
    p_mint.add_argument(
        "--user-token",
        dest="service_account",
        action="store_false",
        help="Mint as a user token instead of a service account.",
    )
    p_mint.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON summary instead of the bare token.",
    )
    p_mint.set_defaults(func=_cmd_mint_token)
