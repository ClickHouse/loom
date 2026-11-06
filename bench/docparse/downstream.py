"""What a parser's output does downstream: each system's artifacts are ingested
into a running Loom through a stub extractor that serves them, then the same
queries go to `knowledge.search` and, optionally, the same documents to
decision extraction.

`search`: for each corpus file, up to three paragraphs (a run of eight words
from each is the query) and up to two table rows (the row's first cell and its
third), each searched within its own document (`uri_prefix`),
since what a parse decides is what its chunks hold and cite, not how documents
rank against each other. A query is found at k when a top-k hit holds most
(70%) of the answer's words, and cited when that hit's locator
also holds the answer's page and spans no more pages than the answer does plus
one (a chunk packs neighbouring blocks); a table row's, a page or two within the
table's pages (the gold has no row's page). DOCX has no pages: found is cited.

`decisions` (with `--decisions N`): the first N PDF documents (PEPs first) are
mined with `extract_document` in a dry run; a claim's quote is kept only when it
is in the parsed text, so the share of proposed claims kept is the quote-match
rate, and the share of kept quotes in the gold text is how often a parse's text
is the document's.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from bench.bootstrap import paired_bootstrap
from bench.docparse.score import words

FORMATS = ("pdf", "docx", "pptx")
INGEST_DEADLINE_S = 1800  # one system's ingest of the corpus


# ---- a normalized parse as an artifact, for a parser that writes only that


def artifact_from_parse(parse: dict, fmt: str) -> dict:
    """A contract artifact from a normalized parse (`run.py`'s shape): its
    blocks in order with their pages, headings opening sections by level, and
    its tables both as table blocks and as `tables`, every value text."""
    blocks: list[dict] = []
    stack: list[tuple[int, str, str]] = []  # (level, title, block id)
    for b in parse.get("blocks", []):
        text = b["text"]
        if not text.strip():
            continue
        bid = f"b{len(blocks)}"
        loc = {}
        if b.get("page_start"):
            loc = {"page_start": b["page_start"], "page_end": max(b["page_end"], b["page_start"])}
        out: dict[str, Any] = {"id": bid, "type": "paragraph", "text": text}
        if b["type"] == "heading":
            level = max(1, int(b.get("level") or 1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][2] if stack else ""
            stack.append((level, text[:500], bid))
            out.update(type="heading", depth=level)
        else:
            parent = stack[-1][2] if stack else ""
            if b["type"] in ("list", "code"):
                out["type"] = b["type"]
            if b["type"] == "table":
                rows = text.split("\n")
                out["attrs"] = {
                    "table": {"rows": len(rows), "columns": max(len(r.split(" | ")) for r in rows)}
                }
        if stack:
            out["section_path"] = [t for _, t, _ in stack]
        if parent:
            out["parent"] = parent
        if loc:
            out["locator"] = loc
        blocks.append(out)
    tables = []
    for k, t in enumerate(parse.get("tables", [])):
        rows = [[str(c) for c in r] for r in t["rows"]]
        if len(rows) < 2:
            continue
        names: list[str] = []
        for i, c in enumerate(rows[0]):
            name = c[:500] or f"column {i + 1}"
            while name in names:
                name += " (2)"
            names.append(name)
        width = len(names)
        body = [(r + [""] * width)[:width] for r in rows[1:]]
        table: dict[str, Any] = {
            "id": f"t{k}",
            "name": f"table {k + 1}",
            "columns": [{"name": n, "type": "string"} for n in names],
            "rows": body,
            "header_rows": 1,
        }
        if t.get("page_start"):
            table["locator"] = {
                "page_start": t["page_start"],
                "page_end": max(t["page_end"], t["page_start"]),
            }
        tables.append(table)
    pages = max([b.get("page_end", 0) for b in parse.get("blocks", [])] + [1])
    complete = bool(parse.get("complete"))
    # the units the parse says failed, or the document when it names none
    failed = [
        {"unit": str(f.get("unit", "document")), "reason": str(f.get("reason", ""))}
        for f in parse.get("failed", [])
    ]
    if not complete and not failed:
        failed = [{"unit": "document", "reason": "the parser says it is incomplete"}]
    coverage = {
        "unit": "pages",
        "total": pages,
        "extracted": pages if complete else max(0, pages - len(failed)),
        "failed": failed,
        "lost": [],
        "complete": complete,
    }
    return {
        "schema_version": 1,
        "format": fmt,
        "parser": "normalized-parse",
        "parser_version": "1",
        "options": {},
        "attrs": {},
        "warnings": [],
        "coverage": coverage,
        "blocks": blocks,
        **({"tables": tables} if tables else {}),
    }


# ---- the stub extractor


class _Stub(ThreadingHTTPServer):
    """Serves the artifacts of the system `self.system` names, by file name."""

    daemon_threads = True

    def __init__(self, port: int, token: str, systems: dict[str, Path]) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.token, self.systems, self.system = token, systems, ""
        self.refused = 0  # requests with another token: the server is set up with a different one


class _Handler(BaseHTTPRequestHandler):
    server: _Stub

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("authorization") != f"Bearer {self.server.token}":
            self.server.refused += 1
            self._send(401, b'{"error": {"code": "unauthorized", "message": "token"}}')
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 — the stdlib's handler name
        if not self._authorized():
            return
        caps = {
            "contract_version": 1,
            "extractor": {"name": "docparse-stub", "version": "1"},
            "max_bytes": 64 * 1024 * 1024,
            "formats": [{"format": f, "options": []} for f in FORMATS],
        }
        self._send(200, json.dumps(caps).encode())

    def do_POST(self) -> None:  # noqa: N802 — the stdlib's handler name
        if not self._authorized():
            return
        self.rfile.read(int(self.headers.get("content-length", "0")))
        name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
        path = self.server.systems[self.server.system] / f"{name}.json"
        if Path(name).name != name or not path.is_file():  # a corpus file's name, no path
            self._send(422, b'{"error": {"code": "corrupt", "message": "no artifact"}}')
            return
        self._send(200, path.read_bytes())


# ---- ingest and search


def _send(client: httpx.Client, method: str, url: str, **kw: Any) -> httpx.Response:
    """A request, waiting out a 429 as long as its Retry-After says."""
    for _ in range(60):
        r = client.request(method, url, timeout=300, **kw)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        time.sleep(float(r.headers.get("retry-after", "1") or 1))
    raise RuntimeError(f"{url} still answers 429 after 60 tries")


def _post(client: httpx.Client, op: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _send(client, "POST", f"/v1/{op}", json=payload).json()


def ingest(
    client: httpx.Client,
    org: str,
    namespace: str,
    corpus: Path,
    files: list[str],
    refused: Callable[[], int],
) -> dict[str, str]:
    """Upload and ingest each file (partial ones too), waiting for every job:
    each file's final state. A job still running at INGEST_DEADLINE_S, or a
    token the stub refused (`refused()`), stops the run with every open job
    cancelled: the stub would otherwise serve it the next system's artifact."""
    jobs = {}
    for f in files:
        data = (corpus / f).read_bytes()
        params = {
            "org": org,
            "namespace": namespace,
            "filename": f,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        up = _send(client, "POST", "/v1/knowledge.upload", params=params, content=data)
        job = _post(
            client,
            "knowledge.ingest",
            {
                "org": org,
                "namespace": namespace,
                "upload_id": up.json()["upload_id"],
                "source_uri": f"docparse://{f}",
                "title": Path(f).stem,
                "allow_partial": True,
            },
        )
        jobs[f] = job["ingestion_id"]
    states: dict[str, str] = {}
    deadline = time.monotonic() + INGEST_DEADLINE_S
    while len(states) < len(jobs) and time.monotonic() < deadline and not refused():
        for f, jid in jobs.items():
            if f in states:
                continue
            st = _post(
                client,
                "knowledge.ingest_status",
                {"org": org, "namespace": namespace, "ingestion_id": jid},
            )
            if st.get("state") in ("ready", "partial", "failed", "cancelled"):
                states[f] = st["state"]
        time.sleep(1)
    if refused():
        why = "the server sent the stub another token: --token is not its own"
    elif len(states) < len(jobs):
        why = f"{len(jobs) - len(states)} ingests still running after {INGEST_DEADLINE_S} s"
    else:
        return states
    for f, jid in jobs.items():
        if f not in states:
            payload = {"org": org, "namespace": namespace, "ingestion_id": jid}
            _post(client, "knowledge.ingest_cancel", payload)
    raise SystemExit(why)


def queries(gold: list[dict]) -> list[dict]:
    """The fixed query set: per file, up to three paragraphs and two table rows."""
    out = []
    for g in gold:
        paras = [b for b in g["blocks"] if b["kind"] == "paragraph" and len(words(b["text"])) >= 12]
        for b in paras[:: max(1, len(paras) // 3)][:3]:
            w = words(b["text"])
            out.append(
                {
                    "file": g["file"],
                    "category": g["category"],
                    "kind": "paragraph",
                    "query": " ".join(w[2:10]),
                    "answer": w,
                    "page": b.get("page", 0),
                    "page_end": b.get("page_end", b.get("page", 0)),
                }
            )
        rows = [(t, r) for t in g["tables"] for r in t["rows"][1:] if len(r) >= 3]
        for t, r in rows[:: max(1, len(rows) // 2)][:2]:
            out.append(
                {
                    "file": g["file"],
                    "category": g["category"],
                    "kind": "table row",
                    # a row by its name and one of its values: names alone (`quill-0`,
                    # `quill-30`) share their words
                    "query": f"{r[0]} {r[2]}",
                    "answer": words(" ".join(r)),
                    # a row's own page is not in the gold: the table's pages are
                    "page": t.get("page_start", 0),
                    "page_end": t.get("page_end", t.get("page_start", 0)),
                    "within": True,
                }
            )
    return out


def judge(q: dict, hits: list[dict], namespace: str) -> dict[str, Any]:
    """Where the answer is found, and cited, in the hits (1-based rank, 0 for
    not): a hit of the query's document in `namespace`, the system's own."""
    found = cited = 0
    want = set(q["answer"])
    for rank, h in enumerate(hits, start=1):
        if h.get("source_uri") != f"docparse://{q['file']}":
            continue
        if h.get("namespace") != namespace:
            continue  # another system's copy of the document
        have = set(words(h.get("content", "")))
        if len(want & have) < 0.7 * len(want):
            continue
        found = found or rank
        loc = h.get("locator") or {}
        lo, hi = loc.get("page_start", 0), loc.get("page_end", 0) or loc.get("page_start", 0)
        if q.get("within"):  # a table row: cited within the table's pages, a page or two at once
            right = q["page"] <= lo <= hi <= q["page_end"] and hi - lo <= 1
        else:
            right = lo <= q["page"] <= hi and hi - lo <= q["page_end"] - q["page"] + 1
        if not q["page"] or right:
            cited = cited or rank
    return {"found": found, "cited": cited}


def search(
    client: httpx.Client,
    org: str,
    namespace: str,
    qs: list[dict],
    k: int,
    states: dict[str, str],
) -> list[dict]:
    """Each query's judgement. A query of a file whose ingest this run did not
    finish (`states`) is not found: an earlier run's revision may still serve."""
    out = []
    for q in qs:
        if states.get(q["file"]) not in ("ready", "partial"):
            out.append({"found": 0, "cited": 0})
            continue
        # within its own document (the parse decides what chunks hold and cite) and
        # its system's own namespace (every system's copy has the same URI)
        payload = {
            "org": org,
            "namespace": namespace,
            "namespace_scope": "this",
            "query": q["query"],
            "top_k": k,
            "uri_prefix": [f"docparse://{q['file']}"],
        }
        res = _post(client, "knowledge.search", payload)
        out.append(judge(q, res.get("results", []), namespace))
    return out


def report(systems: list[str], qs: list[dict], results: dict[str, list[dict]], k: int) -> dict:
    """Recall at 1 and k, found and cited, per category and overall, each
    system after the first paired with it (files resampled)."""
    groups = {
        c: [i for i, q in enumerate(qs) if q["category"] == c]
        for c in sorted({q["category"] for q in qs})
    }
    groups["ALL"] = list(range(len(qs)))
    out: dict = {}
    for grp, idx in groups.items():
        out[grp] = {"n": len(idx)}
        for metric, at in (("cited@1", 1), (f"cited@{k}", k), (f"found@{k}", k)):
            field = "cited" if metric.startswith("cited") else "found"
            vals = {
                s: [1.0 if 0 < results[s][i][field] <= at else 0.0 for i in idx] for s in systems
            }
            row: dict[str, Any] = {s: sum(v) / len(v) for s, v in vals.items()}
            for s in systems[1:]:
                b = paired_bootstrap(
                    [a - c for a, c in zip(vals[s], vals[systems[0]], strict=True)],
                    [qs[i]["file"] for i in idx],
                )
                row[f"{s}-{systems[0]}"] = [b.mean, b.lb, b.ub]
            out[grp][metric] = row
    return out


def namespace_of(system: str) -> str:
    """The namespace a system's documents are ingested into."""
    return f"docparse-{re.sub(r'[^a-z0-9-]', '-', system.lower())}"


DECISION_FILES = [f"pep{n}.pdf" for n in (518, 621, 668, 723, 735, 691)] + [
    f"web_doc_{k}.pdf" for k in range(6)
]


def run(
    base_url: str,
    org: str,
    corpus: Path,
    systems: dict[str, Path],
    stub_port: int,
    token: str,
    k: int = 5,
    decide: int = 0,
) -> dict:
    """Ingest each system's artifacts into its own namespace (`docparse-<name>`)
    through the stub on `stub_port`, run the query set, and with `decide` mine
    the first `decide` of DECISION_FILES; the report of each. A Loom with auth
    off, on this host (the stub binds 127.0.0.1). A file whose ingest is still
    running at the deadline stops the run: the stub would serve it the next
    system's artifact."""
    gold = [json.loads(line) for line in (corpus / "gold.jsonl").read_text().splitlines()]
    files = [g["file"] for g in gold]
    qs = queries(gold)
    namespaces = {name: namespace_of(name) for name in systems}
    stub = _Stub(stub_port, token, systems)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    results: dict[str, list[dict]] = {}
    states: dict[str, dict[str, str]] = {}
    try:
        with httpx.Client(base_url=base_url) as client:
            for name, namespace in namespaces.items():
                stub.system = name
                states[name] = ingest(
                    client, org, namespace, corpus, files, refused=lambda: stub.refused
                )
                results[name] = search(client, org, namespace, qs, k, states[name])
    finally:
        stub.shutdown()
        stub.server_close()
    out: dict[str, Any] = {
        "states": states,
        "queries": qs,
        "results": results,
        "report": report(list(systems), qs, results, k),
    }
    if decide:
        by_file = {g["file"]: g for g in gold}
        chosen = DECISION_FILES[:decide]
        gold_text = {f"docparse://{f}": words(_gold_text(by_file[f])) for f in chosen}
        out["decisions"] = decisions(
            org, namespaces, [f"docparse://{f}" for f in chosen], gold_text, states
        )
    return out


def _gold_text(g: dict) -> str:
    return " ".join(
        [b["text"] for b in g["blocks"]]
        + [" ".join(c for r in t["rows"] for c in r) for t in g["tables"]]
    )


# ---- decision extraction


def decisions(
    org: str,
    namespaces: dict[str, str],
    uris: list[str],
    gold_text: dict[str, list[str]],
    states: dict[str, dict[str, str]],
) -> dict:
    """Mine each document of each system's namespace in a dry run and count, at
    the point extraction checks a claim's quotes against the parsed text
    (`decision_extract.ground`): the claims proposed, the claims whose quotes
    are in the parsed text, and the quotes found there that are also in the
    gold text (in order, as words). Runs in this process, so it needs the
    server's settings (`LOOM_CH_*` and a real `LOOM_LLM_PROVIDER`). A
    document whose ingest this run did not finish (`states`, by system and
    file) is not mined, and is counted `unread`: an earlier run's revision may
    still serve it."""
    from server.cli._ch import run_connected
    from server.memory.app import decision_extract as app
    from server.memory.domain import decision_extract as dx

    counts: dict[str, dict[str, int]] = {}
    current: list[str] = [""]
    real = dx.ground

    def counted(cand: Any, texts: list[str]) -> Any:
        grounded, notes = real(cand, texts)
        c = counts[current[0]]
        c["claims"] += 1 + len(cand.reasons) + len(cand.alternatives)
        if grounded is not None:
            c["kept"] += 1 + len(grounded.reasons) + len(grounded.alternatives)
            for q in grounded.quotes():
                c["quotes"] += 1
                c["in_gold"] += _in(words(q), gold_text[current[1]])
        return grounded, notes

    async def _run(_ch: Any) -> int:
        for system, namespace in namespaces.items():
            counts[system] = {
                "docs": 0,
                "claims": 0,
                "kept": 0,
                "quotes": 0,
                "in_gold": 0,
                "errors": 0,
                "unread": 0,
            }
            for uri in uris:
                if states[system].get(uri.removeprefix("docparse://")) not in ("ready", "partial"):
                    counts[system]["unread"] += 1
                    continue
                current[:] = [system, uri]
                mined = await app.extract_document(org, namespace, uri, dry_run=True)
                counts[system]["docs"] += 1
                counts[system]["errors"] += bool(mined.error)
        return 0

    dx.ground = counted  # the extractor reads it off the module at each call
    try:
        run_connected(_run)
    finally:
        dx.ground = real
    out: dict[str, dict[str, Any]] = {}
    for system, c in counts.items():
        # None where nothing was measured (every document failed, no quote kept)
        out[system] = {
            **c,
            "quote_match": c["kept"] / c["claims"] if c["claims"] else None,
            "in_gold_rate": c["in_gold"] / c["quotes"] if c["quotes"] else None,
        }
    return out


def _in(probe: list[str], stream: list[str]) -> int:
    """1 when `probe` is a run of `stream`'s words."""
    n = len(probe)
    if not n:
        return 0
    first = probe[0]
    return int(any(stream[i : i + n] == probe for i, w in enumerate(stream) if w == first))
