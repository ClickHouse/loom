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
from server.core.pg import AuthorityMismatch


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
    from server.auth import keyring
    from server.auth.authz import GRANT_ROLES
    from server.auth.jwt import mint_token, new_jti
    from server.core.principals import RESERVED_SUBJECT, is_reserved_subject

    if is_reserved_subject(args.subject):
        # Before the keyring read: no such token can be minted, connected or not.
        print(f"  ✗ {RESERVED_SUBJECT}: {args.subject!r}", file=sys.stderr)
        return 2
    refused = _platform_admin_refusal(args) if args.platform_admin else None
    if refused:
        print(f"  ✗ {refused}", file=sys.stderr)
        return 2
    if args.platform_admin and not _platform_admin_rollout_ready():
        print(
            "  ✗ platform-admin tokens require a readable fleet with the rollout complete",
            file=sys.stderr,
        )
        return 1
    try:
        keyring.load_for_offline_mint(run=run_connected)
    except ValueError as e:  # another root: no server would accept the token
        print(f"  ✗ {e}", file=sys.stderr)
        return 1
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
        if args.platform_admin:  # the role reaches every namespace unless told otherwise
            namespaces = namespaces or ["*"]
        if not namespaces:
            print(
                "  ✗ one of --namespaces or --namespace-role is required",
                file=sys.stderr,
            )
            return 2
        roles = ["platform_admin"] if args.platform_admin else args.roles
        token = mint_token(
            subject=args.subject,
            org=args.org,
            namespaces=namespaces,
            roles=roles,
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
            "roles": roles,
            "ttl_days": args.ttl_days,
            "is_service_account": args.service_account,
            "read_scope": args.read_scope or "namespace",
            "read_user_id": args.read_user_id or "",
            "traffic_class": args.traffic_class or "",
            "consumer": args.consumer or "",
        }

    if args.platform_admin and not _audit_platform_admin_mint(args, summary):
        # Never handed out unrecorded: the role spans every org.
        print("  ✗ the mint could not be recorded in the audit log; no token", file=sys.stderr)
        return 1
    return _emit_minted_token(args, token, summary)


# not a setting: the longest a minted platform-admin token may live; longer is a bot's job
_PLATFORM_ADMIN_MAX_DAYS = 90


def _platform_admin_refusal(args: argparse.Namespace) -> str | None:
    """Why `--platform-admin` cannot mint as asked, or None. The role spans every
    org, so it goes to a service account alone, as a flat token that expires."""
    if not args.subject.startswith("sa:"):
        return "--platform-admin mints a service account's token: pass --subject sa:<name>"
    if not args.service_account:
        return "--platform-admin mints a service account's token, never a --user-token"
    if not args.ttl_days or args.ttl_days < 1:
        return "--platform-admin needs --ttl-days: a token holding it may not live for ever"
    if args.ttl_days > _PLATFORM_ADMIN_MAX_DAYS:
        return (
            f"--platform-admin mints for at most {_PLATFORM_ADMIN_MAX_DAYS} days; for longer, "
            "register a bot (`loom bot create --platform-admin`)"
        )
    from server.auth import agents

    if agents.parse(args.subject) is not None:
        return "--platform-admin never mints an agent's token (sa:<trust>:<agent>)"
    if args.namespace_role:
        return "--platform-admin mints a flat token: pass no --namespace-role"
    return None


def _platform_admin_rollout_ready() -> bool:
    from server.auth import machines

    try:
        return bool(run_connected(machines.platform_admin_tokens_ready))
    except AuthorityMismatch:
        raise  # a shell configured for another deployment: its own one-line refusal
    except Exception as e:  # noqa: BLE001 — an unread fleet refuses an admin token
        print(f"  ✗ fleet state could not be read: {type(e).__name__}", file=sys.stderr)
        return False


def _audit_platform_admin_mint(args: argparse.Namespace, summary: dict[str, Any]) -> bool:
    """Record a platform-admin mint before it is handed out; False when the
    audit row could not be written."""
    import getpass

    from server.observability import audit

    async def _emit(ch: Any) -> str:
        return await audit.emit(
            "auth.platform_admin_minted",
            org=args.org,
            namespace="-",
            actor=f"cli:{getpass.getuser()}",
            subject={
                "subject": args.subject,
                "jti": summary["jti"],
                "namespaces": summary["namespaces"],
                "ttl_days": args.ttl_days,
            },
        )

    try:
        return bool(run_connected(_emit))
    except Exception as e:  # noqa: BLE001 — an unrecorded mint is refused, never handed out
        print(f"  ✗ audit log not reached: {type(e).__name__}: {e}", file=sys.stderr)
        return False


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


def _revocation_floor(current: dict[str, Any]) -> Any:
    from server.auth import revocation
    from server.core.datetimes import as_aware_utc, to_naive_utc

    return to_naive_utc(
        current["issued_before"]
        or revocation.past_every_token_before(as_aware_utc(current["revoked_at"]))
    )


async def _revise_revocation(
    subject: str, *, expires_at: Any, issued_before: Any, reason: str, by: str
) -> dict[str, Any] | None:
    """Keep previously retired tokens dead when changing a revocation."""
    from server.auth import revocation

    async with revocation.subject_change(subject):
        current = await revocation.stored(subject)
        if current is not None and revocation.never_lapses(current):
            if expires_at is not None or (
                issued_before is not None and issued_before < _revocation_floor(current)
            ):
                print(
                    "  ✗ a permanent revocation cannot expire or restore previously retired tokens",
                    file=sys.stderr,
                )
                return None
        if issued_before is not None:
            scim_row = (await revocation.scim_revocations([subject])).get(subject)
            if scim_row is not None and scim_row["issued_before"] is None:
                print(
                    f"  ! SCIM also revokes {subject!r} outright, which this leaves: "
                    "--scim gives it back",
                    file=sys.stderr,
                )
        return await revocation.revoke(
            subject,
            expires_at=expires_at,
            issued_before=issued_before,
            reason=reason,
            revoked_by=by,
        )


async def _scim_give_back(
    subject: str, issued_before: Any, reason: str, by: str
) -> tuple[dict[str, Any] | None, int]:
    """`--scim`: narrow SCIM's revocation of `subject` to the tokens issued
    before `issued_before`. The row written, or None and the exit code."""
    from server.auth import revocation, scim
    from server.core import leases

    try:  # SCIM's own lease on the subject: never amid its lift of it
        async with scim.subject_leases([subject]):
            current = (await revocation.scim_revocations([subject])).get(subject)
            if current is None:
                print(f"  ✗ SCIM has not revoked {subject!r}", file=sys.stderr)
                return None, 1
            floor = _revocation_floor(current)
            if issued_before < floor:
                print(
                    f"  ✗ --issued-before must be at or after {floor.isoformat()}Z: "
                    "SCIM's revocation keeps the tokens issued before that dead",
                    file=sys.stderr,
                )
                return None, 2
            row = await revocation.scim_revoke(
                subject, issued_before=issued_before, reason=reason, revoked_by=by
            )
            return row, 0
    except scim.Busy as e:
        if isinstance(e.__cause__, leases.LeaseUnavailable):
            print("  ✗ the lease store did not answer; retry once it does", file=sys.stderr)
        else:
            print(f"  ✗ SCIM is changing {subject!r}; retry shortly", file=sys.stderr)
        return None, 1


def _cmd_revoke_token(args: argparse.Namespace) -> int:
    """Revoke one token through `server.auth.revocation`, the
    same path the `/v1/me/service_accounts.revoke` handler and the refresh
    worker use — never a hand INSERT. `verify_token` checks a bearer's `jti`
    and its `sub` against the one revoked column, so `--jti` retires an
    IdP-minted JWT or a `loom mint-token` token (each carries one; mint-token
    prints it) and `--subject` retires every token minted for that
    principal, or, as `<org>/user:<id>`, every token of that end user in that
    org. Other replicas see the row on their next refresh tick.
    `--subject S --issued-before T` retires only the tokens
    minted for S before T, so a token re-minted after the leak keeps working;
    with `--scim` it narrows SCIM's revocation of S instead (a subject a
    deactivation caught that is someone else's), waiting up to 10 s while SCIM
    changes S. `--list` prints the unexpired revocations. A `--subject`
    revocation with neither `--issued-before` nor `--expires-at` is for good,
    as `POST /v1/admin/revoke_subject` writes it (`server.auth.subject_revocation`):
    the tokens the subject made are also listed revoked. Run again, it writes
    nothing and says the subject is already revoked; the first reason stays."""
    import getpass

    from server.auth import revocation, subject_revocation
    from server.core.principals import org_of_end_user_revocation

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
    subject = (args.subject or "").strip()
    end_user_org = org_of_end_user_revocation(subject)
    problem = subject_revocation.refusal(subject) if args.subject is not None else None
    if problem:
        print(f"  ✗ {problem}", file=sys.stderr)
        return 2
    if args.scim and (issued_before is None or expires_at is not None):
        print(
            "  ✗ --scim narrows SCIM's revocation, which never lapses; pass --issued-before "
            "and no --expires-at",
            file=sys.stderr,
        )
        return 2
    for_good = bool(args.subject) and not args.scim and expires_at is None and issued_before is None

    async def _run(ch: Any) -> int:
        if args.list:
            return _print_revocations(await revocation.list_active(), as_json=args.json)
        token_id = (args.jti or subject).strip()
        by = f"cli:{getpass.getuser()}"
        kind = "jti" if args.jti else "subject"
        # A CLI revoke has no request ledger row; its audit row is its only
        # durable record. A revocation spans every org ('-'), but an end
        # user's belongs to their org alone.
        org = end_user_org or "-"
        counts: dict[str, int] = {}
        kept = False
        if args.scim:
            row, failed = await _scim_give_back(token_id, issued_before, args.reason or "", by)
            if row is None:
                return failed
        elif for_good:
            done = await _revoke_for_good(token_id, by=by, org=org, reason=args.reason or "")
            if isinstance(done, int):
                return done
            row, kept = done.row, done.kept
            counts = {"sessions": done.sessions, "tokens": done.tokens}
        else:
            row = await _revise_revocation(
                token_id,
                expires_at=expires_at,
                reason=args.reason or "",
                by=by,
                issued_before=issued_before,
            )
            if row is None:
                return 2
        if not for_good:
            await subject_revocation.audit_revocation(
                row, token_id=token_id, kind=kind, by=by, org=org, scim=args.scim
            )
        _print_revoked(args, row, kind, token_id, end_user_org, issued_before, counts, kept)
        return 0

    try:
        return run_connected(_run)
    except AuthorityMismatch:
        raise  # a shell configured for another deployment: its own one-line refusal
    except Exception as e:  # noqa: BLE001 — a revocation that did not land must say so
        print(f"  ✗ revocation not recorded: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


async def _revoke_for_good(subject: str, *, by: str, org: str, reason: str) -> Any:
    """`revoke-token --subject` for good through the shared steps: the
    `Revoked`, or the exit code once a failure is said as what it left."""
    from server.auth import subject_revocation

    try:
        row, kept = await subject_revocation.record(subject, by=by, reason=reason)
    except Exception as e:  # noqa: BLE001 — nothing was written, and it must say so
        print(
            f"  ✗ revocation not recorded, nothing was written: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1
    try:
        return await subject_revocation.finish(subject, row, kept, by=by, org=org)
    except Exception as e:  # noqa: BLE001 — the revocation holds; the marking must be finished
        print(
            f"  ✗ revoked {subject!r}, but marking the tokens it made revoked did not finish "
            f"({type(e).__name__}: {e}); run it again to finish",
            file=sys.stderr,
        )
        return 1


def _print_revoked(
    args: argparse.Namespace,
    row: dict[str, Any],
    kind: str,
    token_id: str,
    end_user_org: str,
    issued_before: Any,
    counts: dict[str, int],
    kept: bool = False,
) -> None:
    """What `revoke-token` wrote, as JSON or one line; `counts` are a revocation
    for good's sessions and tokens, and `kept` says it found one already."""
    from server.settings import settings

    if args.json:
        extra = {**counts, "already_revoked": kept} if counts else {}
        print(json.dumps({**row, "jti": token_id, "matched_claim": kind, **extra}, default=str))
        return
    if kept:  # written before, by whoever and for whatever reason it was
        why = f": {row['reason']}" if row["reason"] else ""
        print(
            f"already revoked {kind} {token_id!r} for good, since "
            f"{str(row['revoked_at'])[:19]} by {row['revoked_by']}{why}; nothing written; "
            f"{counts['sessions']} sign-in session(s) and {counts['tokens']} token(s) it made "
            "are refused, those tokens listed revoked"
        )
        return
    what = "narrowed SCIM's revocation of" if args.scim else "revoked"
    print(f"{what} {kind} {token_id!r}", end="")
    end_user = bool(end_user_org)
    where = "that org's end user" if end_user else "in every org"
    if kind == "subject" and issued_before is not None:
        print(
            f" for tokens issued before {issued_before.isoformat()}Z "
            f"({where}; a token minted after that stays valid)",
            end="",
        )
    elif kind == "subject" and end_user:
        print(" (that org's end user: every token its issuer signs for them)", end="")
    elif kind == "subject":
        print(" (every token whose sub is this principal, in every org", end="")
        if counts:
            print(
                f"; {counts['sessions']} sign-in session(s) and {counts['tokens']} token(s) "
                "it made end with it, those tokens listed revoked",
                end="",
            )
        print(")", end="")
    print(f"; other replicas pick it up within {settings.jwt_revocation_cache_ttl_seconds:g} s")


def _print_revocations(rows: list[dict[str, Any]], *, as_json: bool) -> int:
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
    marks = {"cutoff": "cutoff→", "scim": "scim→"}
    for r in rows:
        before = r.get("issued_before")
        key = marks.get(str(r.get("kind")), "") + str(r.get("jti", ""))
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
        "carries one; a gateway's token may not — use --subject.",
    )
    which.add_argument(
        "--subject",
        default=None,
        help="The `sub` claim: retires EVERY token minted for this principal, in "
        "every org (verification checks sub against the revoked set); narrow it "
        "with --issued-before. An end user is <org>/user:<id>: their tokens in that "
        "org alone.",
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
        "--scim",
        action="store_true",
        help="With --subject and --issued-before: narrow SCIM's revocation of the "
        "subject instead of its own, to give back a subject a SCIM deactivation "
        "caught that is someone else's. Waits up to 10 s while SCIM is changing "
        "the subject.",
    )
    p_revoke.add_argument(
        "--reason", default=None, help="Free text stored on the row (audit trail)."
    )
    p_revoke.add_argument(
        "--expires-at",
        default=None,
        metavar="ISO8601",
        help="When the revocation may lapse: no earlier than the last expiry of the "
        "tokens it must end (up to a day for a bot allowed one). Omit to keep the row "
        "for good (to 2105).",
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
        "--platform-admin",
        action="store_true",
        help="Mint the platform_admin role, which spans every org, instead of --roles: "
        "for a service account (--subject sa:<name>, never an agent's) and with --ttl-days "
        "(at most 90); each mint is audited, and the token covers every namespace unless "
        "--namespaces names some.",
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
