"""Push a bench dataset into Langfuse, and record a run of it.

`bench/README.md` holds the recorded retrieval baseline as pasted stdout. That is
enough to notice a regression if someone remembers to look, and nothing more: no
history, no diff between two runs, no way to see WHICH twelve queries moved.
Langfuse datasets exist for exactly this, and loom already writes traces and
scores there.

Two entry points:

    # once per dataset (idempotent — re-run after adding items)
    python -m bench.langfuse_dataset push /path/to/retrieval_qa.json

    # after a bench run, from bench/retrieval_recall.py --langfuse-run <name>
    python -m bench.langfuse_dataset run --help

Deliberately standalone, and using `urllib` rather than the loom server's
Langfuse client: it runs from a laptop or a scheduled job, against a control
plane that may be remote, and must not require importing the server package or a
running event loop.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_HOST = "https://us.cloud.langfuse.com"


def _credentials() -> tuple[str, str]:
    """(host, basic-auth-header). Accepts the same env loom itself uses."""
    host = (
        os.environ.get("LANGFUSE_HOST") or os.environ.get("LOOM_LANGFUSE_URL") or DEFAULT_HOST
    ).rstrip("/")
    token = os.environ.get("LOOM_LANGFUSE_ADMIN_TOKEN", "")
    if not token:
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
        if pk and sk:
            token = f"{pk}:{sk}"
    if ":" not in token:
        raise SystemExit(
            "set LOOM_LANGFUSE_ADMIN_TOKEN='pk-lf-...:sk-lf-...' (or "
            "LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY)"
        )
    return host, "Basic " + base64.b64encode(token.encode()).decode()


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    host, auth = _credentials()
    req = urllib.request.Request(
        host + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400].decode(errors="replace")


def push(dataset_path: str) -> int:
    """Create the dataset and its items. Idempotent per item.

    Langfuse dedupes dataset items on `id`, so the id is derived from the query
    rather than generated: re-running after editing a note updates that item in
    place instead of creating a second copy of the whole set.
    """
    spec = json.loads(Path(dataset_path).read_text())
    questions = spec.get("questions") or []
    if not questions:
        print(f"no questions in {dataset_path}", file=sys.stderr)
        return 1

    name = Path(dataset_path).stem
    st, body = _call(
        "POST",
        "/api/public/v2/datasets",
        {
            "name": name,
            "description": (spec.get("_about") or "")[:1000],
            "metadata": {
                # The caveats travel WITH the data. A 12-item smoke set read as a
                # benchmark is how a number nobody should quote ends up in a deck.
                "corpus": spec.get("_corpus", ""),
                "method": spec.get("_method", ""),
                "caveat": spec.get("_caveat", ""),
                "org": spec.get("org", ""),
                "namespace": spec.get("namespace", ""),
                "source_file": dataset_path,
            },
        },
    )
    if st >= 300:
        print(f"dataset create failed: {st} {body}", file=sys.stderr)
        return 1
    print(f"dataset {name!r}: ready")

    ok = 0
    for q in questions:
        query = q.get("query") or ""
        if not query:
            continue
        st, body = _call(
            "POST",
            "/api/public/dataset-items",
            {
                "datasetName": name,
                # Stable id → re-running updates instead of duplicating.
                "id": f"{name}:{_slug(query)}",
                "input": {"query": query},
                "expectedOutput": {"gold_keys": q.get("gold_keys") or []},
                "metadata": {"note": q.get("note", "")},
            },
        )
        if st >= 300:
            print(f"  ! {query[:50]}: {st} {body}", file=sys.stderr)
            continue
        ok += 1
    print(f"dataset {name!r}: {ok}/{len(questions)} items upserted")
    return 0 if ok == len(questions) else 1


def _slug(text: str) -> str:
    keep = [c if c.isalnum() else "-" for c in text.lower()]
    return "".join(keep).strip("-")[:80]


def record_run(
    *,
    dataset_name: str,
    run_name: str,
    items: list[dict[str, Any]],
    run_metadata: dict[str, Any] | None = None,
) -> int:
    """Attach one bench run's per-query results to the dataset.

    `items` is a list of `{"query", "trace_id", "scores": {name: value}}`.

    A dataset-run-item links a dataset item to the trace the run produced, which
    is what makes the Langfuse run-comparison view work — two runs side by side,
    per query, instead of two pasted stdout blocks.
    """
    linked = 0
    scored = 0
    for it in items:
        query = it.get("query") or ""
        trace_id = it.get("trace_id") or ""
        if not query or not trace_id:
            continue
        st, body = _call(
            "POST",
            "/api/public/dataset-run-items",
            {
                "runName": run_name,
                "datasetItemId": f"{dataset_name}:{_slug(query)}",
                "traceId": trace_id,
                "metadata": run_metadata or {},
            },
        )
        if st >= 300:
            print(f"  ! run-item {query[:40]}: {st} {body}", file=sys.stderr)
            continue
        linked += 1
        for score_name, value in (it.get("scores") or {}).items():
            st, body = _call(
                "POST",
                "/api/public/scores",
                {
                    "traceId": trace_id,
                    "name": score_name,
                    "value": value,
                    "dataType": "NUMERIC",
                    "comment": f"bench run {run_name}",
                },
            )
            if st < 300:
                scored += 1
    print(f"run {run_name!r}: linked {linked} items, wrote {scored} scores")
    return 0 if linked else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push", help="Create/update the dataset and its items.")
    p_push.add_argument("dataset", help="Path to a bench dataset JSON file.")

    p_run = sub.add_parser("run", help="Record a run from a results JSON file.")
    p_run.add_argument("--dataset-name", required=True)
    p_run.add_argument("--run-name", required=True)
    p_run.add_argument(
        "--results",
        required=True,
        help='JSON object {"items": [{"query","trace_id","scores":{...}}], "metadata": {...}}, '
        "as bench/retrieval_recall.py --langfuse-results writes it.",
    )

    args = p.parse_args()
    if args.cmd == "push":
        return push(args.dataset)
    payload = json.loads(Path(args.results).read_text())
    return record_run(
        dataset_name=args.dataset_name,
        run_name=args.run_name,
        items=payload.get("items") or [],
        run_metadata=payload.get("metadata") or {},
    )


if __name__ == "__main__":
    raise SystemExit(main())
