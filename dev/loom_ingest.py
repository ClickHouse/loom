#!/usr/bin/env python3
"""loom-ingest — drop a file into the knowledge plane.

The file's bytes go to `POST /v1/knowledge.upload` as they are, and
`knowledge.ingest` has the server's extractor service parse them and
publish the document's next revision. This CLI parses nothing: what a
format becomes (sections, tables, page locators) is the extractor's,
and a format no configured extractor serves fails the job with
`format_unavailable`. By default it waits for each job and prints how
it ended.

Supported sources:
  - Local filesystem path
  - s3://bucket/key                             — single S3 object
  - s3://bucket/prefix/                         — list + loop (one ingest per object)
  - https://example.com/page.html               — URL (its media type names the
                                                  format when the path has no extension)

NFS / EFS / Lustre mounts work for free as local paths.

Auth: reads ~/.loom/token (mode 0600) by default. Override with --token
or $LOOM_API_KEY. S3 source uses the standard boto3 credential chain
(env vars, ~/.aws/credentials, IAM role).

Usage:
  loom-ingest path/to/CLAUDE.md                     # tags=[], title=CLAUDE
  loom-ingest --tags coding,team-policy doc.pdf
  loom-ingest --namespace project-x --title "Q3 spec" spec.md
  loom-ingest --source-uri 'github://x/spec.md' --tags rfc spec.md
  loom-ingest --no-wait big.xlsx                    # print the job id and return
  loom-ingest s3://acme-docs/runbooks/r1.pdf        # single S3 object
  loom-ingest s3://acme-docs/runbooks/                # all objects under prefix

Exit codes:
  0   every file ingested (or queued, with --no-wait)
  1   a fetch, upload or job failed
  2   bad arguments
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

_S3_SCHEME = "s3://"
_URL_FETCH_TIMEOUT_S = 30
_URL_MAX_BYTES = 10 * 1024 * 1024  # 10 MB cap; refuse larger


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "path",
        help="Local file OR `s3://bucket/key` (single object) "
        "OR `s3://bucket/prefix/` (loop over objects) "
        "OR `https://example.com/...` (URL fetch)",
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("LOOM_BASE_URL"),
        help="Loom server URL (default: $LOOM_BASE_URL)",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("LOOM_API_KEY"),
        help="JWT (default: $LOOM_API_KEY or ~/.loom/token)",
    )
    p.add_argument("--org", help="Org. Default: derived from JWT claim 'org'")
    p.add_argument("--namespace", help="Namespace. Default: first claim 'namespaces' entry")
    p.add_argument("--title", help="Document title (default: the file name less its extension)")
    p.add_argument("--tags", default="", help="Comma-separated tag list (default: none)")
    p.add_argument("--source-uri", help="Source identifier (default: file://<absolute-path>)")
    p.add_argument(
        "--format",
        default="",
        help="The file's format when its name does not say (markdown, html, text, sql, ...)",
    )
    p.add_argument(
        "--allow-partial",
        action="store_true",
        help="Publish an extraction that lost part of the file, marked partial (default: fail)",
    )
    p.add_argument(
        "--no-wait", action="store_true", help="Print each job's id instead of waiting for it"
    )
    p.add_argument(
        "--timeout", type=float, default=600.0, help="Seconds to wait for each job (default 600)"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Print what would be uploaded without sending it"
    )
    return p.parse_args(argv)


def _resolve_token(args: argparse.Namespace) -> str:
    """--token / $LOOM_API_KEY (argparse default), else the SDK's token file."""
    if args.token:
        return args.token
    from loom.transport import token_from_file

    token = token_from_file()
    if token:
        return token
    print(
        "No token: pass --token, set $LOOM_API_KEY, or write one to ~/.loom/token", file=sys.stderr
    )
    sys.exit(2)


def _resolve_identity(args: argparse.Namespace, token: str) -> tuple[str, str]:
    """Org + namespace from the CLI, falling back to the token's claims."""
    if args.org and args.namespace:
        return args.org, args.namespace
    from loom.token import decode_token_claims

    claims = decode_token_claims(token)
    if claims is None:
        print("Could not decode the token as a JWT. Pass --org and --namespace.", file=sys.stderr)
        sys.exit(2)
    org = args.org or claims.org
    namespace = args.namespace or (claims.namespaces[0] if claims.namespaces else None)
    if not org or not namespace:
        print("JWT did not carry org / namespaces. Pass --org and --namespace.", file=sys.stderr)
        sys.exit(2)
    return org, namespace


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """`s3://bucket/key/path` → (bucket, key). Empty key → prefix list mode."""
    rest = uri[len(_S3_SCHEME) :]
    if "/" not in rest:
        return rest, ""
    bucket, key = rest.split("/", 1)
    return bucket, key


def _is_s3_prefix(uri: str) -> bool:
    """A prefix is anything ending with `/` or with no extension on the
    final segment: a key with no extension is listed as a prefix, and the
    objects found are named by their stored media type."""
    _, key = _parse_s3_uri(uri)
    # No key at all (`s3://acme.docs`) is the whole bucket, a dot in its name or not.
    return not key or key.endswith("/") or "." not in key.rsplit("/", 1)[-1]


def _s3_client():
    """Lazy boto3 import — error message is the actionable hint when
    the user hasn't installed the ingest extra."""
    try:
        import boto3
    except ImportError:
        print(
            "S3 ingest requires boto3: pip install 'clickhouse-loom-server[ingest]'",
            file=sys.stderr,
        )
        sys.exit(1)
    return boto3.client("s3")


def _list_s3_prefix(uri: str) -> list[str]:
    """Return absolute s3:// URIs for every object under a prefix.
    Filters out zero-byte 'directory marker' keys."""
    bucket, prefix = _parse_s3_uri(uri)
    client = _s3_client()
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if key.endswith("/"):  # directory marker, not an object
                continue
            keys.append(f"{_S3_SCHEME}{bucket}/{key}")
    return keys


def _is_url(uri: str) -> bool:
    return uri.startswith("http://") or uri.startswith("https://")


class _Stop(Exception):
    """A refusal: `code` is the exit status, the message is printed."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _File:
    """One file as it is uploaded: its bytes, the name the server reads its
    format from, the media type (a URL's or an S3 object's), and the source_uri it
    defaults to."""

    data: bytes
    filename: str
    content_type: str
    source_uri: str


def _fetch_s3_object(uri: str) -> _File:
    """One object's bytes and its stored media type, which names the format of
    a key with no extension."""
    bucket, key = _parse_s3_uri(uri)  # a URI with no key is a prefix (`_is_s3_prefix`)
    obj = _s3_client().get_object(Bucket=bucket, Key=key)
    return _File(obj["Body"].read(), key.rsplit("/", 1)[-1], obj.get("ContentType") or "", uri)


def _fetch_url(uri: str) -> _File:
    """Fetch a URL (30 s timeout, 10 MB cap). The file name is the path's last
    segment; with no extension there, the response's media type names the format."""
    import httpx

    headers = {"User-Agent": "loom-ingest/1.0 (+https://github.com/ClickHouse/loom)"}
    try:
        with httpx.Client(
            timeout=_URL_FETCH_TIMEOUT_S, follow_redirects=True, headers=headers
        ) as client:
            r = client.get(uri)
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"URL fetch failed: {e}") from e
    if len(r.content) > _URL_MAX_BYTES:
        raise RuntimeError(
            f"URL response too large ({len(r.content)} bytes); cap is {_URL_MAX_BYTES}. "
            "Save it to a file and ingest that."
        )
    ctype = (r.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
    # From the path alone: a host such as `example.com` is no file name.
    name = unquote(urlsplit(uri).path).rstrip("/").rsplit("/", 1)[-1] or "index"
    return _File(r.content, name, ctype, uri)


def _materialize(uri: str) -> _File:
    if uri.startswith(_S3_SCHEME):
        return _fetch_s3_object(uri)
    if _is_url(uri):
        return _fetch_url(uri)
    path = Path(uri)
    return _File(path.read_bytes(), path.name, "", f"file://{uri}")


def _plan(args: argparse.Namespace) -> list[str]:
    """The source URIs this run ingests: every object under an S3 prefix,
    else the one path or URL. Raises _Stop(2) when a flag that names ONE
    document (--source-uri, --title) is given for many."""
    raw = args.path
    if raw.startswith(_S3_SCHEME) and _is_s3_prefix(raw):
        if args.source_uri or args.title:
            raise _Stop(
                2,
                "--source-uri and --title name one document; an S3 prefix ingests many "
                "(each keeps its own s3:// URI and file name)",
            )
        uris = _list_s3_prefix(raw)
        if not uris:
            raise _Stop(1, f"no objects found under {raw}")
        print(f"ingesting {len(uris)} object(s) from {raw}")
        return uris
    if raw.startswith(_S3_SCHEME) or _is_url(raw):
        return [raw]
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise _Stop(2, f"not a file: {path}")
    return [str(path)]


def _ingest_args(args: argparse.Namespace, file: _File) -> dict[str, Any]:
    """`knowledge.ingest`'s fields for one file, past its upload_id."""
    return {
        "source_uri": args.source_uri or file.source_uri,
        "title": args.title or Path(file.filename).stem,
        "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
        "metadata": {"ingested_from": file.source_uri},
        "allow_partial": args.allow_partial,
        "options": {"format": args.format} if args.format else {},
    }


def _print_dry_run(file: _File, fields: dict[str, Any]) -> None:
    print(
        f"would upload {file.filename!r} ({len(file.data)} bytes, sha256 "
        f"{hashlib.sha256(file.data).hexdigest()}) and ingest it as:"
    )
    print(json.dumps(fields, indent=2))


def _report(source_uri: str, status: dict[str, Any]) -> bool:
    """Print how a job ended; whether it published."""
    state = status.get("state", "")
    if state in ("ready", "partial"):
        print(
            f"ingested → source_uri={source_uri} revision={status.get('revision', '?')} "
            f"chunks={status.get('chunks', '?')} format={status.get('format', '?')} state={state}"
        )
        for warning in status.get("warnings") or []:
            print(f"  warning: {warning}", file=sys.stderr)
        return True
    why = status.get("error_message") or status.get("error_code") or "no such job"
    code = f" ({status['error_code']})" if status.get("error_code") else ""
    print(f"✗ {source_uri}: {state or 'not found'}{code}: {why}", file=sys.stderr)
    return False


async def _ingest_one(agent: Any, args: argparse.Namespace, file: _File) -> bool:
    """Upload one file and ingest it; whether it published (or was queued)."""
    fields = _ingest_args(args, file)
    if args.dry_run:
        _print_dry_run(file, fields)
        return True
    up = await agent.knowledge.upload(file.data, file.filename, content_type=file.content_type)
    job = await agent.knowledge.ingest(up["upload_id"], **fields)
    if args.no_wait:
        print(f"queued → ingestion_id={job['ingestion_id']} source_uri={fields['source_uri']}")
        return True
    status = await agent.knowledge.wait_for_ingest(job["ingestion_id"], timeout_s=args.timeout)
    return _report(fields["source_uri"], status)


async def _ingest_all(
    args: argparse.Namespace, uris: list[str], token: str, org: str, namespace: str
) -> int:
    from loom import Agent
    from loom.errors import LoomError

    failures = 0
    async with Agent(org, namespace, "loom-ingest", base_url=args.base_url, api_key=token) as agent:
        for uri in uris:
            try:
                file = _materialize(uri)
            except Exception as e:  # noqa: BLE001 — one unreadable object must not stop the rest
                print(f"✗ {uri}: {e}", file=sys.stderr)
                failures += 1
                continue
            try:
                ok = await _ingest_one(agent, args, file)
            except (LoomError, TimeoutError) as e:
                print(f"✗ {uri}: {e}", file=sys.stderr)
                ok = False
            failures += not ok
    if failures:
        print(f"✗ {failures} of {len(uris)} failed", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.base_url:
        print("LOOM_BASE_URL not set; pass --base-url", file=sys.stderr)
        return 2
    token = _resolve_token(args)
    org, namespace = _resolve_identity(args, token)
    try:
        uris = _plan(args)
    except _Stop as e:
        print(str(e), file=sys.stderr)
        return e.code
    return asyncio.run(_ingest_all(args, uris, token, org, namespace))


if __name__ == "__main__":
    raise SystemExit(main())
