"""`loom knowledge source list|add|update`: a namespace's document sources over a
running server's `knowledge.source.*` ops, as the token's principal. Items are
given as pasted links (`--link`) or as JSON objects (`--item-json`); a credential
is read from a file, never from the command line."""

from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any

from server.cli._client import (
    default_base_url,
    detail_line,
    jwt_claims,
    post_op,
    refused_for_agent,
    resolve_token,
)


def register_knowledge_source_cli(ksub: Any) -> None:
    p = ksub.add_parser("source", help="A namespace's document sources, over a running server.")
    ssub = p.add_subparsers(dest="source_command", required=True)
    p_list = ssub.add_parser("list", help="List the namespace's sources, items and sync state.")
    _common(p_list)
    p_list.set_defaults(func=_cmd_list)
    p_add = ssub.add_parser(
        "add",
        help="Register a source from links or items, probed with the credential before "
        "anything is stored (knowledge.source.add).",
    )
    _common(p_add)
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--kind", required=True, help="gdrive, notion or linear")
    _items(p_add)
    _include(p_add)
    p_add.add_argument(
        "--credential-file",
        required=True,
        help="The file holding the source's read-only credential; - reads it from stdin",
    )
    p_add.add_argument("--dry-run", action="store_true", help="Probe and store nothing")
    p_add.set_defaults(func=_cmd_add)
    p_update = ssub.add_parser(
        "update",
        help="Add and remove a source's items or change its file kinds, the added items "
        "probed with the stored credential (knowledge.source.update).",
    )
    _common(p_update)
    p_update.add_argument("--name", required=True)
    _items(p_update)
    _include(p_update)
    _items(p_update, prefix="remove-", verb="remove")
    p_update.set_defaults(func=_cmd_update)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--namespace", required=True, help="The namespace the source's documents live in"
    )
    p.add_argument("--org", default="", help="Default: the token's org")
    p.add_argument("--server-url", default=default_base_url())
    p.add_argument("--token", default=None, help="JWT (default: $LOOM_API_KEY, ~/.loom/token)")


def _items(p: argparse.ArgumentParser, prefix: str = "", verb: str = "read") -> None:
    p.add_argument(
        f"--{prefix}link", action="append", default=[], help=f"A link to {verb}; repeatable"
    )
    p.add_argument(
        f"--{prefix}item-json",
        action="append",
        default=[],
        help=f'An item to {verb} as JSON, such as {{"type": "folder", "id": "..."}}; repeatable',
    )


def _include(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--include",
        action="append",
        default=None,
        help="gdrive: a file kind to read (docs, slides, pdf, pptx, docx); repeatable",
    )


def _item_objects(values: list[str], flag: str = "--item-json") -> list[dict[str, Any]] | None:
    """The items as objects, or None (with the reason printed) when one is not one."""
    out = []
    for value in values:
        try:
            item = json.loads(value)
        except ValueError:
            item = None
        if not isinstance(item, dict):
            print(f"{flag}: not a JSON object: {value[:80]}", file=sys.stderr)
            return None
        out.append(item)
    return out


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def _call(args: argparse.Namespace, op: str, body: dict[str, Any]) -> int:
    token, source = resolve_token(args.token)
    if refused_for_agent(source):
        return 2
    org = args.org or str(jwt_claims(token).get("org") or "")
    if not org:
        print("Could not infer org from the token; pass --org.", file=sys.stderr)
        return 2
    status, out = post_op(
        args.server_url, op, {"org": org, "namespace": args.namespace, **body}, token
    )
    if status == 0:
        print(f"{op} → no Loom answer: {out.get('detail', '')}", file=sys.stderr)
        return 1
    if status != 200:
        phrase = _phrase(status)
        detail = out.get("detail")
        # A string detail here is another server's page (a proxy's), never printed.
        line = detail_line(status, phrase, None if isinstance(detail, str) else detail)
        print(f"{op} → {line}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    return _call(args, "knowledge.source.list", {})


def _cmd_add(args: argparse.Namespace) -> int:
    if not args.link and not args.item_json:
        print("give --link or --item-json", file=sys.stderr)
        return 2
    items = _item_objects(args.item_json)
    if items is None:
        return 2
    path = args.credential_file
    try:
        credential = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        why = "not UTF-8 text" if isinstance(e, UnicodeDecodeError) else e.strerror
        print(f"--credential-file: cannot read {path} ({why})", file=sys.stderr)
        return 2
    body: dict[str, Any] = {
        "name": args.name,
        "kind": args.kind,
        "credential": credential.strip(),
        "dry_run": args.dry_run,
    }
    if items:
        body["items"] = items
    if args.link:
        body["links"] = args.link
    if args.include:
        body["include"] = args.include
    return _call(args, "knowledge.source.add", body)


def _cmd_update(args: argparse.Namespace) -> int:
    added = _item_objects(args.item_json)
    removed = _item_objects(args.remove_item_json, "--remove-item-json")
    if added is None or removed is None:
        return 2
    body: dict[str, Any] = {
        "name": args.name,
        "add_items": added,
        "add_links": args.link,
        "remove_items": removed,
        "remove_links": args.remove_link,
    }
    if args.include:
        body["include"] = args.include
    return _call(args, "knowledge.source.update", body)


__all__ = ["register_knowledge_source_cli"]
