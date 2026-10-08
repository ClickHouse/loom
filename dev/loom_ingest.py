#!/usr/bin/env python3
"""loom-ingest — drop a local file into the knowledge plane.

The wire op `knowledge.add` takes `content: str` — callers have to
read + parse the file themselves. This CLI does the parse step
locally and POSTs the extracted text, giving operators a one-liner
for "drop a CLAUDE.md / specs.pdf / runbook.md into my datapack."

Supported formats:
  - Markdown / plain text / JSON / CSV / YAML  — read as UTF-8, no parsing
  - PDF                                         — pypdf text extraction; the
                                                  document outline (bookmarks /
                                                  table of contents) becomes
                                                  Markdown headings so the
                                                  knowledge plane chunks by
                                                  section

Supported sources:
  - Local filesystem path
  - s3://bucket/key                             — single S3 object
  - s3://bucket/prefix/                         — list + loop (one ingest per object)
  - https://example.com/page.html               — URL (HTML / PDF / md / text)

NFS / EFS / Lustre mounts work for free as local paths. Out of scope
(intentional): Parquet, Iceberg, image, video. Each needs a separate
parser + ingestion pipeline; if a tenant asks, we add them one at a
time rather than commit to a multi-format pipeline now.

Auth: reads ~/.loom/token (mode 0600) by default. Override with --token
or $LOOM_API_KEY. S3 source uses the standard boto3 credential chain
(env vars, ~/.aws/credentials, IAM role).

Usage:
  loom-ingest path/to/CLAUDE.md                     # tags=[], title=filename
  loom-ingest --tags coding,team-policy doc.pdf
  loom-ingest --namespace project-x --title "Q3 spec" spec.md
  loom-ingest --source-uri 'github://x/spec.md' --tags rfc spec.md
  loom-ingest s3://acme-docs/runbooks/r1.pdf        # single S3 object
  loom-ingest s3://acme-docs/runbooks/                # all objects under prefix

Exit codes:
  0   added successfully
  1   parse / network failure
  2   bad arguments
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_TEXT_LIKE_EXTS = {".md", ".markdown", ".txt", ".json", ".csv", ".yaml", ".yml", ".rst", ".log"}

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
        "OR `https://example.com/...` (URL fetch + extract)",
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
    p.add_argument("--title", help="Document title (default: filename)")
    p.add_argument("--tags", default="", help="Comma-separated tag list (default: none)")
    p.add_argument("--source-uri", help="Source identifier (default: file://<absolute-path>)")
    p.add_argument(
        "--upsert",
        action="store_true",
        help="Use knowledge.upsert (supersede prior versions) instead of add",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Parse + print the request body without posting"
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
    final segment. Single objects always have a file extension worth
    parsing."""
    if uri.endswith("/"):
        return True
    last_segment = uri.rsplit("/", 1)[-1]
    return "." not in last_segment


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


def _fetch_s3_object(uri: str) -> Path:
    """Download one S3 object to a temp file. Caller unlinks when done."""
    bucket, key = _parse_s3_uri(uri)
    if not key:
        print(f"S3 URI missing key: {uri}", file=sys.stderr)
        sys.exit(2)
    client = _s3_client()
    # Preserve extension so _extract_text can dispatch on it.
    suffix = Path(key).suffix
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="loom-ingest-")
    os.close(fd)
    client.download_file(bucket, key, tmp_path)
    return Path(tmp_path)


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


def _fetch_url(uri: str) -> tuple[Path, str]:
    """Fetch a URL, return (local_path, content_type).

    PDFs (Content-Type: application/pdf, OR URL ends in .pdf) land at
    a temp file with .pdf suffix so the existing extension dispatcher
    routes them through pypdf. HTML and other text formats land at a
    temp file with the extension inferred from Content-Type / URL.

    Defenses: 30s timeout, 10MB size cap (refuse larger), explicit
    user-agent identifying Loom.
    """
    try:
        import httpx
    except ImportError:
        print(
            "URL ingest requires httpx (server dep — should already "
            "be installed): pip install httpx",
            file=sys.stderr,
        )
        sys.exit(1)

    headers = {"User-Agent": "loom-ingest/1.0 (+https://github.com/ClickHouse/loom)"}
    try:
        with httpx.Client(
            timeout=_URL_FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers=headers,
        ) as client:
            r = client.get(uri)
            r.raise_for_status()
            ctype = (r.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            body = r.content
    except httpx.HTTPError as e:
        print(f"URL fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    if len(body) > _URL_MAX_BYTES:
        print(
            f"URL response too large ({len(body)} bytes); cap is "
            f"{_URL_MAX_BYTES}. Narrow the URL or save to a file and "
            f"ingest that.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Suffix drives _extract_text dispatch; infer from ctype when URL has none.
    suffix = Path(uri.split("?", 1)[0]).suffix.lower()
    if not suffix:
        if "pdf" in ctype:
            suffix = ".pdf"
        elif "html" in ctype or "xhtml" in ctype:
            suffix = ".html"
        elif "markdown" in ctype:
            suffix = ".md"
        elif "json" in ctype:
            suffix = ".json"
        else:
            suffix = ".txt"

    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="loom-ingest-url-")
    os.close(fd)
    Path(tmp_path).write_bytes(body)
    return Path(tmp_path), ctype


def _extract_html(path: Path) -> str:
    """HTML → clean text via trafilatura. Falls back to a regex strip
    if trafilatura isn't installed — the fallback produces worse
    chunks (boilerplate, nav, footer) but unblocks ingestion."""
    try:
        import trafilatura
    except ImportError:
        print(
            "HTML extraction prefers trafilatura: pip install "
            "'clickhouse-loom-server[ingest]'. Falling back to a "
            "regex strip; expect noisy chunks (boilerplate, nav).",
            file=sys.stderr,
        )
        import re

        html = path.read_text(encoding="utf-8", errors="replace")
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()

    extracted = trafilatura.extract(
        path.read_text(encoding="utf-8", errors="replace"),
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    )
    return extracted or ""


OutlineEntry = tuple[int, str, int]  # (page index, title, depth starting at 1)


def _pdf_outline(reader: Any) -> list[OutlineEntry]:
    """Flatten a pypdf outline (nested lists of Destination objects) into
    (page, title, depth) in document order. Entries whose page cannot be
    resolved are skipped; a broken outline yields []. Never raises."""
    out: list[OutlineEntry] = []

    def walk(items: Any, depth: int) -> None:
        for item in items or []:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            title = str(getattr(item, "title", "") or "").strip()
            if not title:
                continue
            try:
                page = int(reader.get_destination_page_number(item))
            except Exception:  # noqa: BLE001 — dangling destination
                continue
            if page >= 0:
                out.append((page, title, depth))

    try:
        walk(getattr(reader, "outline", None), 1)
    except Exception:  # noqa: BLE001
        return []
    return out


def pdf_pages_to_markdown(pages: list[str], outline: list[OutlineEntry]) -> str:
    """Join extracted PDF pages into one text, inserting a Markdown heading
    for every outline entry so the knowledge plane can chunk by section.

    A heading goes where its section starts: at the first occurrence of the
    title's text on its page (case-insensitive, after any earlier heading on
    that page), else at the top of the page. Depth maps to `#` count, capped
    at six. With no outline the pages are joined as before."""
    if not outline:
        return "\n\n".join(p for p in pages if p.strip())
    by_page: dict[int, list[OutlineEntry]] = {}
    for entry in outline:
        by_page.setdefault(entry[0], []).append(entry)
    parts: list[str] = []
    for i, text in enumerate(pages):
        entries = by_page.get(i, [])
        if not entries:
            if text.strip():
                parts.append(text)
            continue
        cursor = 0
        lowered = text.lower()
        for _page, title, depth in entries:
            heading = f"{'#' * min(max(depth, 1), 6)} {title}"
            pos = lowered.find(title.lower(), cursor) if title else -1
            if pos > cursor:
                before = text[cursor:pos]
                if before.strip():
                    parts.append(before)
                parts.append(heading)
                cursor = pos
            else:
                # Title text not found after the cursor: the heading goes
                # before whatever remains of the page.
                parts.append(heading)
        tail = text[cursor:]
        if tail.strip():
            parts.append(tail)
    return "\n\n".join(p.strip("\n") for p in parts if p.strip())


def _extract_text(path: Path) -> str:
    """Read the file and return its text content.

    Plain-text-ish formats are read as UTF-8. PDFs go through pypdf
    page-by-page, with the document outline rendered as Markdown headings
    at the start of each section (so `knowledge.add` chunks on structure).
    Unrecognized extensions are treated as plain text with a permissive
    UTF-8 decode (errors='replace') — works for arbitrary code/config files
    and prints a warning rather than failing.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            print("PDF ingest requires pypdf: pip install pypdf", file=sys.stderr)
            sys.exit(1)
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        outline = _pdf_outline(reader)
        if outline:
            print(f"pdf: {len(outline)} outline entries -> section headings", file=sys.stderr)
        return pdf_pages_to_markdown(pages, outline)
    if suffix in (".html", ".htm", ".xhtml"):
        return _extract_html(path)
    if suffix in _TEXT_LIKE_EXTS:
        return path.read_text(encoding="utf-8")
    # Unknown extension: permissive decode so binary fragments warn, not crash.
    print(
        f"warning: extension {suffix!r} not in known-text list; "
        f"reading as UTF-8 with errors='replace'",
        file=sys.stderr,
    )
    return path.read_text(encoding="utf-8", errors="replace")


async def _post(agent: Any, upsert: bool, body: dict[str, Any]) -> dict[str, Any]:
    """Send one parsed file through the SDK (auth, retries, 429 pacing)."""
    fields = {k: body[k] for k in ("source_uri", "title", "tags", "metadata")}
    if upsert:
        return await agent.knowledge.upsert(body["content"], **fields)
    return await agent.knowledge.add(body["content"], **fields)


def knowledge_body(
    *,
    local_path: Path,
    source_uri: str,
    content: str,
    org: str,
    namespace: str,
    title: str,
    tags: list[str],
) -> dict[str, Any]:
    """The `knowledge.add` / `knowledge.upsert` request body for one parsed
    file — the one shape this CLI and `loom demo` both post,
    so the demo's document lands with the same title, tags and
    `ingested_*` metadata as `make ingest FILE=…` would give it."""
    return {
        "org": org,
        "namespace": namespace,
        "content": content,
        "source_uri": source_uri,
        "title": title,
        "tags": tags,
        "metadata": {
            "ingested_from": source_uri,
            "ingested_format": local_path.suffix.lower() or "unknown",
            "ingested_bytes": local_path.stat().st_size,
        },
    }


def _build_body(
    local_path: Path, source_uri: str, args: argparse.Namespace, org: str, namespace: str
) -> dict[str, Any]:
    content = _extract_text(local_path)
    if not content.strip():
        print(f"warning: extracted empty text from {source_uri}", file=sys.stderr)
    return knowledge_body(
        local_path=local_path,
        source_uri=source_uri,
        content=content,
        org=org,
        namespace=namespace,
        title=args.title or local_path.stem,
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
    )


def _print_dry_run(op_path: str, body: dict[str, Any]) -> None:
    print(f"would POST {op_path}")
    preview = dict(body)
    if len(preview["content"]) > 300:
        preview["content"] = preview["content"][:300] + f"…<{len(body['content'])} chars total>"
    print(json.dumps(preview, indent=2))


class _Stop(Exception):
    """A planning refusal: `code` is the exit status, the message is printed."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def _materialize(uri: str) -> tuple[Path, str, bool]:
    """(local file, default source_uri, whether the file is a temp copy)."""
    if uri.startswith(_S3_SCHEME):
        return _fetch_s3_object(uri), uri, True
    if _is_url(uri):
        return _fetch_url(uri)[0], uri, True
    return Path(uri), f"file://{uri}", False


async def _ingest_all(
    args: argparse.Namespace, uris: list[str], token: str, org: str, namespace: str
) -> int:
    from loom import Agent
    from loom.errors import LoomError

    op_path = "/v1/knowledge.upsert" if args.upsert else "/v1/knowledge.add"
    failures = 0
    async with Agent(org, namespace, "loom-ingest", base_url=args.base_url, api_key=token) as agent:
        for uri in uris:
            try:
                local, default_uri, temp = _materialize(uri)
            except Exception as e:  # noqa: BLE001 — one unreadable object must not stop the rest
                print(f"✗ {uri}: {e}", file=sys.stderr)
                failures += 1
                continue
            source_uri = args.source_uri or default_uri
            try:
                body = _build_body(local, source_uri, args, org, namespace)
                if args.dry_run:
                    _print_dry_run(op_path, body)
                    continue
                try:
                    result = await _post(agent, args.upsert, body)
                except LoomError as e:
                    print(f"✗ {source_uri}: {e}", file=sys.stderr)
                    failures += 1
                    continue
                print(
                    f"ingested → doc_id={result.get('doc_id', '?')} "
                    f"chunks={result.get('chunks_created', '?')} title={body['title']!r} "
                    f"source_uri={source_uri}"
                )
            finally:
                if temp:
                    local.unlink(missing_ok=True)
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
