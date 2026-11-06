"""`loom demo` — the first five minutes: ingest this repo's own memory file
and ask it a question.

  1. ingest `AGENTS.md` (or `--file`) into the knowledge plane as
     `make ingest FILE=… TAGS=…` does: the file's bytes go to
     `/v1/knowledge.upload`, `knowledge.ingest` has the server's extractor parse
     them, and the demo polls `knowledge.ingest_status` until the job ends; a
     re-run publishes the document's next revision;
  2. ask: `knowledge.synthesize` over that one document — the op that
     synthesizes over knowledge (`memory.ask` reads the memory plane; its
     `source="chunk"` hits are memory chunks, not documents). When
     `/v1/health` says `fake_llm`, synthesis would only echo, so the demo
     runs `knowledge.search` instead, prints the top three hits with their
     scores and says what a real answer needs;
  3. print the answer and the evidence rows (file#chunk, section, score,
     excerpt) as a compact table;
  4. default question "What is the identity tuple on every row?" — answered
     by AGENTS.md's "Invariants" section — `--question` overrides.

Talks to `--base-url` (default http://localhost:7777) with the token from
`LOOM_API_KEY`, then `~/.loom/config.json`, then `~/.loom/token`, else none
(the stock single-user stack needs none). Every HTTP call goes through
`_get_json` / `_post_json` / `_post_bytes` so the unit tests swap them
(`tests/unit/test_cli_demo.py`).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from server.cli._client import (
    DEFAULT_BASE_URL,
    default_base_url,
    get_json,
    jwt_claims,
    post_bytes,
    post_op,
    refused_for_agent,
    resolve_token,
)
from server.core.paths import REPO_ROOT

DEFAULT_FILE = "AGENTS.md"
DEFAULT_QUESTION = "What is the identity tuple on every row?"
DEFAULT_TAGS = "loom,project-memory"
TOP_K = 3
# How long the demo waits for its ingest job, which a worker runs.
INGEST_WAIT_S = 120  # not a setting: the demo's own patience
# The states an ingest job ends in.
_INGEST_DONE = ("ready", "partial", "failed", "cancelled")
REAL_LLM_HINT = (
    "synthesis needs a real LLM: set LOOM_LLM_PROVIDER=openai and OPENAI_API_KEY "
    "(the OpenAI block in .env.example), then `loom up --rebuild`"
)


# ---- HTTP seams ----------------------------------------------------------------


# The tests replace these module names with a scripted server.
_get_json = get_json
_post_json = post_op
_post_bytes = post_bytes
_sleep = time.sleep


def _explain_http(op: str, status: int, body: dict[str, Any], base_url: str) -> str:
    detail = body.get("detail") or body.get("error") or body.get("message") or ""
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail)
    detail = str(detail)[:300]
    if status == 0:
        return f"{op}: cannot reach {base_url} ({detail}) — is the stack up? `loom up` boots it."
    if status == 401:
        return (
            f"{op}: HTTP 401 — this server wants a token. Set LOOM_API_KEY or save one to "
            f"~/.loom/token (`loom up` prints one when auth is on). {detail}"
        )
    if status == 403:
        return f"{op}: HTTP 403 — the token has no writer role on this namespace. {detail}"
    return f"{op}: HTTP {status} {detail}".rstrip()


# ---- token + identity -----------------------------------------------------------


def resolve_identity(args: argparse.Namespace, token: str) -> tuple[str, str]:
    """org + namespace: flags, else the token's claims, else the stock
    single-user pair `dev/local`."""
    claims = jwt_claims(token) if token else {}
    org = args.org or str(claims.get("org") or "") or "dev"
    ns_claim = claims.get("namespaces") or []
    first_ns = str(ns_claim[0]) if isinstance(ns_claim, list) and ns_claim else ""
    namespace = args.namespace or first_ns or "local"
    return org, namespace


# ---- 1. ingest (upload, knowledge.ingest, poll) ----------------------------------


def ingest_file(
    *, base_url: str, token: str, org: str, namespace: str, path: Path, tags: str
) -> tuple[str, dict[str, Any], str]:
    """Upload one file, ingest it and wait for the job, as `dev/loom_ingest.py`
    does. Returns (source_uri, the job's last status, error) — error is '' once
    the job published."""
    source_uri = f"file://{path}"
    params = {"org": org, "namespace": namespace, "filename": path.name}
    status, up = _post_bytes(
        f"{base_url}/v1/knowledge.upload?{urlencode(params)}", path.read_bytes(), token
    )
    if status != 201:
        return source_uri, up, _explain_http("knowledge.upload", status, up, base_url)
    body = {
        "org": org,
        "namespace": namespace,
        "upload_id": up.get("upload_id", ""),
        "source_uri": source_uri,
        "title": path.stem,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "metadata": {"ingested_from": source_uri},
    }
    status, job = _post_json(base_url, "knowledge.ingest", body, token)
    if status != 200:
        return source_uri, job, _explain_http("knowledge.ingest", status, job, base_url)
    ask = {"org": org, "namespace": namespace, "ingestion_id": job.get("ingestion_id", "")}
    deadline = time.monotonic() + INGEST_WAIT_S
    while True:
        status, state = _post_json(base_url, "knowledge.ingest_status", ask, token)
        if status != 200:
            return (
                source_uri,
                state,
                _explain_http("knowledge.ingest_status", status, state, base_url),
            )
        done = not state.get("found") or state.get("state") in _INGEST_DONE
        if done or time.monotonic() >= deadline:
            return source_uri, state, _job_error(state, ask["ingestion_id"])
        _sleep(1.0)


def _job_error(state: dict[str, Any], ingestion_id: str) -> str:
    """'' for a job that published; else what happened and what to do."""
    now = str(state.get("state") or "")
    if now in ("ready", "partial"):
        return ""
    if not state.get("found"):
        return f"knowledge.ingest_status: no job {ingestion_id} in this namespace."
    if now not in _INGEST_DONE:
        return (
            f"knowledge.ingest: the job is still {now or 'unknown'} after {INGEST_WAIT_S}s — a "
            f"worker runs it (LOOM_BACKGROUND_WORKERS); knowledge.ingest_status {ingestion_id} "
            "follows it."
        )
    code = str(state.get("error_code") or "")
    why = f"knowledge.ingest: the job {now}" + (f" ({code})" if code else "")
    why += f": {state.get('error_message')}" if state.get("error_message") else ""
    if code == "format_unavailable":
        why += " — no extractor serves this format; `loom up` starts the reference extractor."
    return why


# ---- 2./3. ask + evidence -----------------------------------------------------------


def search_evidence(
    *, base_url: str, token: str, org: str, namespace: str, question: str, source_uri: str
) -> tuple[list[dict[str, Any]], str]:
    body = {
        "org": org,
        "namespace": namespace,
        "query": question,
        "top_k": TOP_K,
        "namespace_scope": "this",
        "uri_prefix": [source_uri],
    }
    status, out = _post_json(base_url, "knowledge.search", body, token)
    if status != 200:
        return [], _explain_http("knowledge.search", status, out, base_url)
    results = out.get("results")
    return (results if isinstance(results, list) else []), ""


def synthesize(
    *, base_url: str, token: str, org: str, namespace: str, question: str, source_uri: str
) -> tuple[dict[str, Any], str]:
    body = {"org": org, "namespace": namespace, "source_uris": [source_uri], "task": question}
    status, out = _post_json(base_url, "knowledge.synthesize", body, token, timeout=180.0)
    if status != 200:
        return {}, _explain_http("knowledge.synthesize", status, out, base_url)
    return out, ""


def _one_line(text: str, width: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def format_evidence(hits: list[dict[str, Any]], cited: set[str], *, width: int = 72) -> str:
    """The evidence table: one row per hit — rank, score, file#chunk (section),
    excerpt; a `*` marks chunks the synthesized answer cited."""
    lines = [f"  {'#':>2} {'score':>6}  {'source':<28} excerpt"]
    for i, h in enumerate(hits, 1):
        uri = str(h.get("source_uri") or "")
        idx = h.get("chunk_idx", "")
        name = Path(uri.removeprefix("file://")).name or uri
        tag = f"{name}#{idx}"
        mark = "*" if f"{uri}#{idx}" in cited or tag in cited else " "
        section = str(h.get("section") or "")
        source = _one_line(f"{tag} ({section})" if section else tag, 28)
        score = h.get("score") if h.get("score") else h.get("similarity", 0.0)
        lines.append(
            f"{mark} {i:>2} {float(score or 0.0):6.3f}  {source:<28} "
            f"{_one_line(str(h.get('content') or ''), width)}"
        )
    return "\n".join(lines)


# ---- the command -----------------------------------------------------------------------


def _add_demo_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--file",
        default=None,
        help=f"Document to ingest (default: {DEFAULT_FILE} in the cwd, "
        "then the package's checkout).",
    )
    p.add_argument(
        "--question",
        default=DEFAULT_QUESTION,
        help=f"What to ask it (default: {DEFAULT_QUESTION!r}).",
    )
    p.add_argument(
        "--tags",
        default=DEFAULT_TAGS,
        help=f"Comma-separated tags on the document (default {DEFAULT_TAGS}).",
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Loom server (default {DEFAULT_BASE_URL}, or LOOM_BASE_URL).",
    )
    p.add_argument(
        "--token",
        default=None,
        help="JWT; default LOOM_API_KEY, then ~/.loom/config.json, then ~/.loom/token, "
        "then none (single-user).",
    )
    p.add_argument("--org", default=None, help="Org (default: the token's, else dev).")
    p.add_argument(
        "--namespace", default=None, help="Namespace (default: the token's first, else local)."
    )
    p.add_argument(
        "--root",
        default=None,
        help="Checkout root holding AGENTS.md (default: the cwd, then the package's checkout).",
    )
    p.add_argument(
        "--search-only",
        action="store_true",
        help="Skip synthesis even with a real LLM; print the top hits only.",
    )


def _resolve_file(args: argparse.Namespace) -> Path | None:
    if args.file:
        return Path(args.file).expanduser()
    roots = [Path(args.root)] if args.root else [Path.cwd(), REPO_ROOT]
    for root in roots:
        if (root / DEFAULT_FILE).is_file():
            return root / DEFAULT_FILE
    return None


def _print_hits_only(hits: list[dict[str, Any]], question: str, *, fake_llm: bool) -> None:
    print(f"\nTop {len(hits)} knowledge hits for {question!r}:")
    print(format_evidence(hits, set()) if hits else "  (no hits)")
    if fake_llm:
        print(f"\n! this stack runs the fake LLM — {REAL_LLM_HINT}")


def _cmd_demo(args: argparse.Namespace) -> int:
    token, token_from = resolve_token(args.token)
    if refused_for_agent(token_from):
        return 2
    org, namespace = resolve_identity(args, token)
    base_url = args.base_url.rstrip("/")

    status, health = _get_json(f"{base_url}/v1/health", token)
    if status == 0 or not health:
        print(f"✗ {_explain_http('GET /v1/health', status, health, base_url)}", file=sys.stderr)
        return 1
    fake_llm = bool(health.get("fake_llm"))

    path = _resolve_file(args)
    if path is None or not path.is_file():
        wanted = args.file or DEFAULT_FILE
        print(
            f"✗ not a file: {wanted} — pass --file path/to/doc.md (any file the server's "
            "extractor reads: Markdown, text, PDF, ...) or run `loom demo` from the loom checkout.",
            file=sys.stderr,
        )
        return 2
    path = path.resolve()

    print(
        f"→ {base_url}  org={org} namespace={namespace} token={token_from} "
        f"llm={'fake' if fake_llm else str(health.get('llm_model') or 'real')}"
    )
    source_uri, out, err = ingest_file(
        base_url=base_url,
        token=token,
        org=org,
        namespace=namespace,
        path=path,
        tags=args.tags,
    )
    if err:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    print(
        f"✓ ingested {path.name} → revision={out.get('revision', '?')} "
        f"chunks={out.get('chunks', '?')} format={out.get('format', '?')} "
        f"state={out.get('state', '?')} (knowledge.ingest, tags={args.tags})"
    )

    hits, err = search_evidence(
        base_url=base_url,
        token=token,
        org=org,
        namespace=namespace,
        question=args.question,
        source_uri=source_uri,
    )
    if err:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    if fake_llm or args.search_only:
        _print_hits_only(hits, args.question, fake_llm=fake_llm)
        return 0

    synth, err = synthesize(
        base_url=base_url,
        token=token,
        org=org,
        namespace=namespace,
        question=args.question,
        source_uri=source_uri,
    )
    if err:
        print(f"✗ {err}", file=sys.stderr)
        _print_hits_only(hits, args.question, fake_llm=False)
        return 1
    print(f"\nQ: {args.question}")
    if synth.get("abstain"):
        print(f"A: (abstained: {synth.get('abstain_reason') or 'no answer'})")
    else:
        print(f"A: {str(synth.get('answer') or '').strip()}")
    model = synth.get("model") or ""
    print(
        f"   [{synth.get('chunks_read', '?')} chunks read, {synth.get('windows', '?')} window(s)"
        f"{', ' + model if model else ''}]"
    )
    cited = {str(c) for c in (synth.get("citations") or [])}
    print("\nEvidence (* = cited by the answer):")
    print(format_evidence(hits, cited) if hits else "  (no hits)")
    return 0


_DEMO_HELP = (
    "The first five minutes: ingest this repo's AGENTS.md (or --file) into the "
    "knowledge plane the way `make ingest` does, ask it a question, and print the "
    "answer with its evidence rows. On the stock fake-LLM stack it prints the top "
    "knowledge.search hits and says what a real answer needs."
)


def register_demo_cli(sub: Any) -> None:
    p_demo = sub.add_parser("demo", help=_DEMO_HELP)
    _add_demo_flags(p_demo)
    p_demo.set_defaults(func=_cmd_demo)
