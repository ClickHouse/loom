"""`loom signing-key` (the token keyring in loom.signing_keys) and `loom blobs
import` (the one-time move of a filesystem blob volume into loom.document_blobs).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

from server.cli._apply import add_apply_flags
from server.cli._ch import run_connected
from server.core.errors import BackendUnavailableError

_SHA = re.compile(r"^[0-9a-f]{64}$")


def _cmd_signing_key(args: argparse.Namespace) -> int:
    from server.auth import keyring
    from server.settings import settings

    if args.signing_cmd in ("rotate", "import", "record-root") and (
        settings.jwt_secret_is_dev_default()
    ):
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

    return run_connected(_go)


async def _servers_refresh_s(keyring: Any, ch: Any) -> float | None:
    """The slowest keyring refresh interval any serving process recorded; None
    when none did (no process of this build serves yet, or a read-only user)."""
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


def _import_map(args: argparse.Namespace) -> dict[str, str]:
    """The kid → key map to import: `--map-env`, else (as the former build
    read it) `{--active: LOOM_JWT_SECRET}`."""
    from server.settings import settings
    from server.settings._env import json_map_items

    raw = os.environ.get(args.map_env, "")
    if raw.strip():
        return dict(json_map_items(raw, args.map_env, non_empty=True))
    return {args.active: str(settings.jwt_secret)}


async def _signing_key_cmd(args: argparse.Namespace, keyring: Any, ch: Any) -> int:
    from server.settings import settings

    servers = await _servers_refresh_s(keyring, ch)
    if args.signing_cmd == "rotate":
        if servers is not None:
            if _too_soon(args.activate_in, servers, "the servers' slowest"):
                return 1
        elif _too_soon(args.activate_in, settings.jwt_keyring_refresh_seconds, "this host's"):
            return 1
        key = await keyring.rotate(args.algorithm, activate_in_s=args.activate_in, ch=ch)
        print(f"{key.kid} ({key.algorithm}) signs from {key.activate_at:%Y-%m-%d %H:%M:%S}Z")
    elif args.signing_cmd == "import":
        # Processes of this build already serve: they must load the key first.
        if servers is not None and _too_soon(args.activate_in, servers, "the servers' slowest"):
            return 1
        keys = _import_map(args)
        done = await keyring.import_keys(
            keys, args.active, args.algorithm, ch=ch, activate_in_s=args.activate_in
        )
        print(f"imported {', '.join(done) or 'nothing (every kid is already there)'}")
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


def _legacy_files(root: Path) -> dict[tuple[str, str, str], Path]:
    """(org, namespace, sha256) → file, for the `{org}/{namespace}/{sha[:2]}/{sha}`
    layout."""
    return {
        (p.parts[-4], p.parts[-3], p.name): p
        for p in root.glob("*/*/??/*")
        if p.is_file() and _SHA.match(p.name)
    }


def _verified(path: Path | None, sha: str) -> bytes | None:
    """The file's bytes when they hash to `sha`, else None."""
    if path is None:
        return None
    data = path.read_bytes()
    return data if hashlib.sha256(data).hexdigest() == sha else None


async def _import_one(
    ch: Any, org: str, ns: str, uri: str, legacy: str, data: bytes | None
) -> bool:
    """Store the document's original (`data` None: already stored) and repoint
    the document at it, under the namespace's originals claim that erasure
    takes too: the document is re-read under it, so one erased, deleted or
    re-uploaded since the listing is left alone (its file is then deleted with
    the rest).

    The row is written one version above the one read: a delete, re-upload or
    edit (which take no claim) that lands between the read and the write is the
    newer row, and the import is lost rather than the document brought back.
    Each write is fenced on the claim: a lapsed one stops the run before any
    file is deleted."""
    from server.core import leases
    from server.core.ch import sequential_reads
    from server.knowledge.app import originals
    from server.knowledge.store import blobs
    from server.knowledge.store import documents as documents_repo

    async with originals.hold_originals(org, [ns], wait_s=originals.CLAIM_TTL_S, ch=ch) as held:
        with sequential_reads():
            doc = await documents_repo.get_one(org, ns, uri, ch=ch)
        if doc is None or doc.blob_path != legacy:
            return False

        def fence() -> None:
            if not held():
                raise leases.LeaseLost(f"document_originals:{org}/{ns}")

        fence()
        sha = (
            documents_repo.original_sha(legacy)
            if data is None
            else await blobs.put(org, ns, data, ch=ch)
        )
        row = documents_repo.build_document_row(
            doc, blob_path=sha, blob_erased_at=None, version=int(doc.version) + 1
        )
        fence()
        await documents_repo.insert_one(row, ch=ch)
    return True


def _cmd_blobs_import(args: argparse.Namespace) -> int:
    """Copy each live document's original from the old blob directory into
    loom.document_blobs and repoint the document at it (`blob_path` = sha256),
    then delete the directory's files and stamp `blob_erased_at` on the deleted
    and erased documents' rows naming one of them: their erasure could not
    delete the file, and the census counts them as residue until this stamp.

    The old build kept a directory per host, so a file may be on another host.
    A live document whose file is not here keeps its row unstamped and the
    command exits 1; `--accept-missing`, once every host has run the import,
    stamps every legacy row left. A live row stamped that way is still read on
    the next run, so the host that holds its file imports it, and a file that a
    live document names is never deleted unless it was imported."""
    from server.core.ch import sequential_reads
    from server.knowledge.store import blobs
    from server.knowledge.store import documents as documents_repo

    files = _legacy_files(Path(args.blob_dir))
    by_sha = {sha: p for (_o, _n, sha), p in files.items()}

    def _file_of(r: dict[str, Any]) -> tuple[str, Path | None]:
        sha = str(r["blob_path"]).rsplit("/", 1)[-1]
        return sha, files.get((str(r["org"]), str(r["namespace"]), sha)) or by_sha.get(sha)

    async def _go(ch: Any) -> int:
        # Stamped or not: `--accept-missing` on another host stamps rows whose file is here.
        rows = await ch.query_all(
            "SELECT org, namespace, source_uri, blob_path FROM loom.documents FINAL "
            f"WHERE {documents_repo.LEGACY_BLOB_PATH} AND superseded_at IS NULL"
        )
        imported: list[tuple[str, str, str, str]] = []
        missing: list[tuple[str, str, str, str]] = []
        named: set[Path] = set()  # files a live row names that were not imported: never deleted
        for r in rows:
            org, ns, uri, legacy = (
                str(r[k]) for k in ("org", "namespace", "source_uri", "blob_path")
            )
            sha, path = _file_of(r)
            data = _verified(path, sha)
            if data is None and path is not None:
                named.add(path)
            # Another host's import (or this one's, before an edit wrote the old path
            # back) stored the bytes: the row is repointed without the file.
            stored = False
            if data is None:
                with sequential_reads():
                    stored = await blobs.size(org, ns, sha, ch=ch) is not None
            if data is None and not stored:
                missing.append((org, ns, uri, sha))
                print(f"  ✗ {org}/{ns} {uri}: no file here matching {sha}")
                continue
            if args.dry_run or await _import_one(ch, org, ns, uri, legacy, data):
                imported.append((org, ns, uri, legacy))
        # A metadata edit or re-ingest (they take no claim) that read a row before its
        # import writes the old path back. Its original is stored and served by sha;
        # the next run repoints it.
        rewritten = []
        for org, ns, uri, legacy in [] if args.dry_run else imported:
            with sequential_reads():
                doc = await documents_repo.get_one(org, ns, uri, ch=ch)
            if doc is not None and doc.blob_path == legacy:
                rewritten.append(uri)
        verb = "to import" if args.dry_run else "imported"
        moved = len(imported) - len(rewritten)
        print(f"{moved} original(s) {verb}, {len(missing)} without a matching file here")
        keep = [] if args.accept_missing else missing
        doomed = [p for p in files.values() if p not in named]
        if args.dry_run:
            print(f"would delete {len(doomed)} file(s) from {args.blob_dir}")
            return 1 if keep else 0
        for p in doomed:
            p.unlink(missing_ok=True)
        print(f"deleted {len(doomed)} original(s) from {args.blob_dir}")
        await documents_repo.stamp_legacy_originals_gone(
            None if args.accept_missing else [p.name for p in doomed],
            [(o, n, sha) for o, n, _uri, sha in missing] if args.accept_missing else [],
            ch=ch,
        )
        if rewritten:
            print(
                f"✗ {len(rewritten)} document(s) were written with their old path during the "
                "import; their originals are stored: run the import again to repoint them",
                file=sys.stderr,
            )
        if keep:
            print(
                f"✗ {len(keep)} live document(s) kept their old path. Their file may be on "
                "another host: run the import on every host that held a blob directory, "
                "then pass --accept-missing to give up the ones still missing",
                file=sys.stderr,
            )
        return 1 if keep or rewritten else 0

    return run_connected(_go)


def register_portability_cli(sub: Any) -> None:
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
    p_in = sk.add_parser(
        "import",
        help="Copy a kid -> key map (the former LOOM_JWT_SECRETS_JSON, else "
        "{--active: LOOM_JWT_SECRET}) into the keyring, so the tokens it signed keep "
        "verifying; --active signs from now. Under RS256 the map's 'default' is stored as "
        "'default-rs256', which verifies and signs as the old 'default'.",
    )
    p_in.add_argument("--map-env", default="LOOM_JWT_SECRETS_JSON", help="env var with the map")
    p_in.add_argument("--active", required=True, help="the kid that signs new tokens")
    p_in.add_argument("--algorithm", choices=("HS256", "RS256"), default="HS256")
    p_in.add_argument(
        "--activate-in",
        type=float,
        default=0.0,
        help="seconds until --active signs (default 0: run before the new build serves, "
        "while every old process already holds the key). Once new-build processes "
        "serve, at least twice their LOOM_JWT_KEYRING_REFRESH_SECONDS (refused otherwise)",
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

    p_bl = sub.add_parser("blobs", help="Uploaded document originals.")
    bl = p_bl.add_subparsers(dest="blobs_cmd", required=True)
    p_imp = bl.add_parser(
        "import",
        help="Move the originals of a filesystem blob directory (the old LOOM_BLOB_DIR) "
        "into ClickHouse.",
    )
    p_imp.add_argument("blob_dir", help="The old blob directory, e.g. var/blobs")
    add_apply_flags(p_imp, writes="store the originals and repoint the documents")
    p_imp.add_argument(
        "--accept-missing",
        action="store_true",
        help="give up the live documents whose file is missing or corrupt: stamp them like "
        "the rest instead of exiting 1. Run the import on every host that held a blob "
        "directory first; a file a live document names is kept either way",
    )
    p_imp.set_defaults(func=_cmd_blobs_import)
