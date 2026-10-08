"""ClickGap's 30 golden PR fixtures replayed through loom.

The fixtures are not in this repository (the findings are a consumer's
private data): point `--fixtures` or `LOOM_CLICKGAP_GOLDEN_DIR` at a
directory of ClickGap's `tests/golden/h_pr*.json`. They are frozen production findings
of a bot that reviews every merged ClickHouse PR: each carries the
hypothesis (`affected_locations`, `what`, `why`, named callers and the
tests it searched for) and the result of the bot's own deterministic
gates at capture time — `expected.gates.results_by_gate.G1_SOURCE_EXISTS`
("one cited file:line exists at upstream master"), `G4_TEST_SUITE_SEARCHED`
("at least three test searches ran") — plus the maintainer's verdict as
`outcome_category`. Nine are PUSHBACK: the maintainer rejected the bot's
output, so the memory that mirrors each of them (`finding_<id>` on
`clickgap-public`) is a row recall should NOT hand back to the bot the next
time it reviews a PR in that subsystem.

Per fixture the runner asks loom the same four questions and scores the
agreement with the fixture; nothing is gated — the numbers are a baseline
for later runs to move. A call that failed (HTTP error or non-200) is counted
as an error, never as an agreement or a pass, and a run with any error exits 1:

- **G1 vs `code.verify_citations`**: the hypothesis rendered as a draft,
  verified at `at_commit = captured_sha` (the upstream-master commit the
  bot's G1 ran against) and at the PR's merge commit (`code.review_brief`).
  A location exists when the mirror says the path existed at the commit
  and the cited line is within its length — the bot's own G1 rule.
- **`affected_locations` vs `code.impact(files=…)`**: does the index hold
  a function on that path starting at or before the cited line (the unit
  a change at that line would touch), and what verdict does the radius
  carry.
- **G4 vs `code.coverage_claim`**: on the symbols the draft names that the
  index resolved (else the first `src/` path), was the corpus measurable
  (control passed, verdict not `unknown`) — the precondition the bot's G4
  asserts by counting greps.
- **Negative gate** (PUSHBACK only): `memory.search` on `clickgap-public`
  with the query ClickGap builds at analysis time (changed paths tokenised
  + PR title), `auto_touch: false`, `agent: "*"`, top-10: the mirrored
  row must not surface. A positive control (the finding's own title and
  `what`) says whether the row is retrievable at all, so a pass is never
  vacuous.

    PYTHONPATH=. python bench/clickgap_golden_replay.py --base http://127.0.0.1:7973 \\
        --fixtures /path/to/clickgap/tests/golden --token-file token.txt \\
        --out arm.jsonl --md arm.md

Read-only: no memory.set / link / outcome; `memory.search` passes
`auto_touch: false`. Spend: one query embedding per memory.search (≈ 20
calls); no LLM.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import statistics
import sys
import time
from collections import Counter
from typing import Any

import httpx

from bench._http import retry_after_s

_WAITS_429 = 8

FIXTURES_DIR = os.environ.get("LOOM_CLICKGAP_GOLDEN_DIR", "")

# `stale` reasons under which the cited file exists and the line is within
# it — only the unit map disagrees (the bot's G1 counts these as existing).
_EXISTS_WHEN_STALE = frozenset({"line_outside_units", "file_changed_since_commit"})


# ------------------------------------------------------------------ fixtures


def load_fixtures(directory: str = FIXTURES_DIR) -> list[dict[str, Any]]:
    if not directory or not os.path.isdir(directory):
        raise SystemExit(
            "no fixture directory: pass --fixtures or set LOOM_CLICKGAP_GOLDEN_DIR "
            "to ClickGap's tests/golden"
        )
    rows = []
    for path in sorted(glob.glob(os.path.join(directory, "h_pr*.json"))):
        with open(path) as fh:
            rows.append(json.load(fh))
    return rows


def _loads(blob: Any) -> dict[str, Any]:
    if isinstance(blob, dict):
        return blob
    try:
        out = json.loads(blob or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def locations_of(hyp: dict[str, Any]) -> list[tuple[str, int]]:
    """`(path, line)` per cited location — `source_locations` strings and
    `affected_locations` dicts, the bot's `_get_source_location_strings`."""
    out: list[tuple[str, int]] = []
    for loc in hyp.get("source_locations") or []:
        path, _, line = str(loc).rpartition(":")
        if not path:
            path, line = line, ""
        out.append((path, int(line) if line.isdigit() else 0))
    for al in hyp.get("affected_locations") or []:
        if isinstance(al, dict) and al.get("file"):
            line = al.get("line")
            out.append((str(al["file"]), int(line) if isinstance(line, int) else 0))
        elif isinstance(al, str):
            path, _, line = al.rpartition(":")
            out.append((path or line, int(line) if path and line.isdigit() else 0))
    return list(dict.fromkeys(out))


def render_draft(fixture: dict[str, Any]) -> str:
    """The hypothesis as the draft the bot would post: title, what, why,
    root cause, pattern, the cited locations as `path:line`, callers and
    the tests it searched — every citation shape verify_citations reads."""
    finding = fixture.get("finding") or {}
    hyp = _loads(finding.get("hypothesis_json"))
    parts = [str(finding.get("title") or hyp.get("title") or "")]
    for label in ("what", "why", "root_cause", "detected_pattern", "impact", "suggested_fix"):
        if hyp.get(label):
            parts.append(f"{label}: {hyp[label]}")
    parts.append("locations: " + ", ".join(f"{p}:{ln}" if ln else p for p, ln in locations_of(hyp)))
    for label in ("callers_found", "existing_tests_for_area"):
        for row in hyp.get(label) or []:
            parts.append(f"{label}: {row}")
    return "\n".join(parts)


def expected_gate(fixture: dict[str, Any], gate: str) -> bool | None:
    gates = ((fixture.get("expected") or {}).get("gates") or {}).get("results_by_gate") or {}
    value = gates.get(gate)
    return bool(value) if isinstance(value, bool) else None


# ------------------------------------------------------------------ scoring


def location_exists(citation: dict[str, Any], anchored: bool) -> bool | None:
    """The bot's G1 rule for one cited location: the path exists and the
    line is within the file. With a commit anchor the mirror's
    `target.at_commit` is exact; without one the index head decides. None
    when loom could not say (path not indexed, commit unknown)."""
    target = citation.get("target") or {}
    at = target.get("at_commit") if isinstance(target, dict) else None
    line = int(citation.get("start_line") or 0)
    if anchored and isinstance(at, dict) and at.get("checked", True) and "existed" in at:
        if not at.get("existed"):
            return False
        lines = int(at.get("lines") or 0)
        return True if not line or not lines else line <= lines
    verdict, reason = citation.get("verdict"), citation.get("reason") or ""
    if verdict == "resolved":
        return True
    if verdict == "stale":
        return reason in _EXISTS_WHEN_STALE or reason == "line_outside_units"
    if verdict == "ambiguous":
        return None
    return None if reason in ("path_not_indexed", "not_covered") else False


def score_g1(
    payload: dict[str, Any], locations: list[tuple[str, int]], anchored: bool
) -> dict[str, Any]:
    """Per cited location the exists verdict; the fixture-level loom_G1 is
    `any exists` (the bot passes G1 on the first location that exists)."""
    by_text = {
        str(c.get("text")): c for c in payload.get("citations") or [] if c.get("kind") == "location"
    }
    by_path = {
        str(c.get("path")): c
        for c in payload.get("citations") or []
        if c.get("kind") in ("location", "path")
    }
    per: list[dict[str, Any]] = []
    for path, line in locations:
        cit = by_text.get(f"{path}:{line}") or by_path.get(path)
        exists = location_exists(cit, anchored) if cit else None
        per.append(
            {
                "path": path,
                "line": line,
                "exists": exists,
                "verdict": (cit or {}).get("verdict"),
                "reason": (cit or {}).get("reason"),
            }
        )
    known = [p["exists"] for p in per if p["exists"] is not None]
    loom_g1 = any(known) if known else None
    commit = payload.get("at_commit") or {}
    return {
        "loom_g1": loom_g1,
        "locations": per,
        "verdict": payload.get("verdict"),
        "counts": payload.get("counts") or {},
        "commit_known": bool(commit.get("sha")) and not commit.get("error"),
        "git_calls": commit.get("calls"),
        "ms": payload.get("_ms"),
        "error": payload.get("_error"),
    }


def locate_touched(payload: dict[str, Any], locations: list[tuple[str, int]]) -> dict[str, Any]:
    """For each cited location, the touched symbol on that path that starts
    at or before the line (nearest); `located` counts locations with one."""
    touched = payload.get("touched_symbols") or []
    rows: list[dict[str, Any]] = []
    for path, line in locations:
        cands = [t for t in touched if str(t.get("path")) == path]
        below = [t for t in cands if not line or int(t.get("start_line") or 0) <= line]
        best = max(below, key=lambda t: int(t.get("start_line") or 0)) if below else None
        rows.append(
            {
                "path": path,
                "line": line,
                "on_path": len(cands),
                "unit": (best or {}).get("qualified_name"),
                "unit_start": (best or {}).get("start_line"),
            }
        )
    return {
        "located": sum(1 for r in rows if r["unit"]),
        "n": len(rows),
        "rows": rows,
        # an empty `verdict` is the radius with callers (only the empty-radius
        # classes carry one), so the hop counts say how many
        "verdict": payload.get("verdict"),
        "impacted": sum(int(v or 0) for v in (payload.get("by_hop_counts") or {}).values()),
        "impacted_tests": len(payload.get("impacted_tests") or []),
        "paths_seen": Counter(str(p.get("class")) for p in payload.get("paths_seen") or []),
        "ms": payload.get("_ms"),
        "error": payload.get("_error"),
    }


def coverage_subjects(
    vc_payload: dict[str, Any], locations: list[tuple[str, int]], cap: int = 3
) -> list[dict[str, str]]:
    """The symbols the draft names that the index resolved (first-appearance
    order, at most `cap`); else the first `src/` path the draft cites."""
    names = [
        str(c.get("text"))
        for c in vc_payload.get("citations") or []
        if c.get("kind") == "name" and c.get("verdict") in ("resolved", "ambiguous")
    ]
    if names:
        return [{"symbol": n} for n in list(dict.fromkeys(names))[:cap]]
    for path, _ in locations:
        if path.startswith("src/"):
            return [{"path": path}]
    return [{"path": locations[0][0]}] if locations else []


def coverage_measurable(payload: dict[str, Any]) -> bool:
    control = payload.get("control") or {}
    return bool(control.get("ok")) and payload.get("verdict") in ("covered_by", "uncovered")


def clickgap_recall_query(paths: list[str], title: str, body: str = "") -> str:
    """ClickGap's analysis-time recall query (`infra/feedback_context.py`):
    up to ten changed paths with separators blanked, the PR title, the
    first 200 characters of the body."""
    parts = [p.replace("/", " ").replace(".", " ").replace("_", " ") for p in paths[:10]]
    if title:
        parts.append(title)
    if body:
        parts.append(body[:200].replace("#", "").replace("-", " "))
    return " ".join(parts)


def surfaced_rank(payload: dict[str, Any], key: str) -> int | None:
    for i, hit in enumerate(payload.get("results") or [], start=1):
        if str(hit.get("memory_key")) == key:
            return i
    return None


# ------------------------------------------------------------------ client


class Client:
    def __init__(self, base: str, token: str, *, consumer: str, org: str, concurrency: int) -> None:
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
        self.consumer, self.org = consumer, org
        self.sem = asyncio.Semaphore(concurrency)
        self.http = httpx.AsyncClient(timeout=180)
        self.errors: Counter[str] = Counter()
        self.ms: dict[str, list[float]] = {}

    async def call(self, op: str, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = {"org": self.org, "namespace": namespace, "consumer": self.consumer, **body}
        async with self.sem:
            # A 429 is the bench share of the namespace's budget, not an answer: wait it out
            # (Retry-After) before a call counts as an error. The latency is the last attempt's.
            for waited in range(_WAITS_429 + 1):
                started = time.monotonic()  # the server's wall, not the queue behind the semaphore
                try:
                    resp = await self.http.post(
                        f"{self.base}/v1/{op}", json=payload, headers=self.headers
                    )
                    data = resp.json() if resp.content else {}
                    if resp.status_code != 200:
                        data = {"_error": data.get("detail") or data, "_status": resp.status_code}
                except (httpx.HTTPError, ValueError) as e:
                    data = {"_error": repr(e)[:200], "_status": -1}
                    resp = None
                elapsed = (time.monotonic() - started) * 1000.0
                if resp is None or resp.status_code != 429 or waited == _WAITS_429:
                    break
                await asyncio.sleep(retry_after_s(resp))
        data["_ms"] = round(elapsed, 1)
        if "_error" in data:
            self.errors[op] += 1
        self.ms.setdefault(op, []).append(elapsed)
        return data

    async def aclose(self) -> None:
        await self.http.aclose()


# ------------------------------------------------------------------ one fixture


async def _merge_facts(client: Client, ns: str, tracker_ns: str, pr: int) -> dict[str, Any]:
    brief, item = await asyncio.gather(
        client.call("code.review_brief", ns, {"pr_number": pr, "tier": "trivial"}),
        client.call("tracker.item", tracker_ns, {"numbers": [pr]}),
    )
    rows = ((brief.get("sections") or {}).get("paths") or {}).get("rows") or []
    items = item.get("items") or []
    return {
        "merge_sha": str(brief.get("merge_sha") or ""),
        "brief_source": brief.get("source"),
        "changed_paths": [str(r.get("path")) for r in rows if r.get("path")],
        "pr_title": str(items[0].get("title") or "") if items else "",
    }


async def _g1_legs(
    client: Client, ns: str, text: str, locations: list[tuple[str, int]], anchors: dict[str, str]
) -> dict[str, Any]:
    async def one(name: str, sha: str) -> tuple[str, dict[str, Any]]:
        body: dict[str, Any] = {"text": text}
        if sha:
            body["at_commit"] = sha
        payload = await client.call("code.verify_citations", ns, body)
        scored = score_g1(payload, locations, anchored=bool(sha))
        scored["anchor"], scored["sha"] = name, sha[:12]
        return name, {**scored, "_payload": payload}

    results = await asyncio.gather(*(one(n, s) for n, s in anchors.items()))
    return dict(results)


async def _coverage_leg(
    client: Client, ns: str, vc_payload: dict[str, Any], locations: list[tuple[str, int]]
) -> list[dict[str, Any]]:
    subjects = coverage_subjects(vc_payload, locations)
    payloads = await asyncio.gather(*(client.call("code.coverage_claim", ns, s) for s in subjects))
    return [
        {
            "subject": s,
            "verdict": p.get("verdict"),
            "tests_total": p.get("tests_total"),
            "epistemic": p.get("epistemic"),
            "control_ok": bool((p.get("control") or {}).get("ok")),
            "measurable": coverage_measurable(p),
            "ms": p.get("_ms"),
            "error": p.get("_error"),
        }
        for s, p in zip(subjects, payloads, strict=True)
    ]


async def _negative_leg(
    client: Client, mem_ns: str, fixture: dict[str, Any], facts: dict[str, Any], hyp: dict[str, Any]
) -> dict[str, Any]:
    key = f"finding_{fixture['fixture_id']}"
    # the PR's changed paths + title as ClickGap sends them; when the ledger
    # has neither (an old PR, a tracker read past its budget) the finding's own
    # cited paths stand in — same subsystem, never the finding's prose
    paths = facts["changed_paths"] or [p for p, _ in locations_of(hyp)]
    source = "pr_paths" if facts["changed_paths"] else "finding_paths"
    query = clickgap_recall_query(paths, facts["pr_title"])
    control_q = " ".join(
        filter(None, [str(fixture["finding"].get("title") or ""), str(hyp.get("what") or "")])
    )
    body = {"top_k": 10, "auto_touch": False, "agent": "*", "include_tags": True}
    neg, ctl = await asyncio.gather(
        client.call("memory.search", mem_ns, {**body, "query": query}),
        client.call("memory.search", mem_ns, {**body, "query": control_q}),
    )
    return {
        "key": key,
        "query": query[:200],
        "query_source": source + ("+title" if facts["pr_title"] else ""),
        "rank": surfaced_rank(neg, key),
        "results": len(neg.get("results") or []),
        "control_rank": surfaced_rank(ctl, key),
        "control_results": len(ctl.get("results") or []),
        "ms": neg.get("_ms"),
        "control_ms": ctl.get("_ms"),
        "error": neg.get("_error") or ctl.get("_error"),
    }


async def replay_fixture(
    client: Client, fixture: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    finding = fixture["finding"]
    hyp = _loads(finding.get("hypothesis_json"))
    locations = locations_of(hyp)
    pr = int(fixture.get("captured_from", {}).get("pr_number") or finding.get("pr_number") or 0)
    facts = await _merge_facts(client, args.namespace, args.tracker_namespace, pr)
    anchors = {"captured": str(fixture.get("captured_sha") or "")}
    if facts["merge_sha"]:  # without a merge sha the leg would read index head as G1@merge
        anchors["merge"] = facts["merge_sha"]
    g1 = await _g1_legs(client, args.namespace, render_draft(fixture), locations, anchors)
    vc_head = g1["captured"]["_payload"]
    files = list(dict.fromkeys(p for p, _ in locations))[:50]
    impact_p, coverage = await asyncio.gather(
        client.call(
            "code.impact", args.namespace, {"files": files, "summary_only": True, "depth": 1}
        ),
        _coverage_leg(client, args.namespace, vc_head, locations),
    )
    row: dict[str, Any] = {
        "fixture_id": fixture["fixture_id"],
        "pr": pr,
        "outcome_category": fixture.get("outcome_category"),
        "is_pushback_seed": bool(fixture.get("is_pushback_seed")),
        "subsystem": finding.get("subsystem"),
        "category": finding.get("category"),
        "state": finding.get("state"),
        "expected_g1": expected_gate(fixture, "G1_SOURCE_EXISTS"),
        "expected_g4": expected_gate(fixture, "G4_TEST_SUITE_SEARCHED"),
        "merge": facts,
        "g1": {k: {kk: vv for kk, vv in v.items() if kk != "_payload"} for k, v in g1.items()},
        "impact": locate_touched(impact_p, locations),
        "coverage": coverage,
    }
    if fixture.get("is_pushback_seed") and not args.skip_memory:
        row["negative"] = await _negative_leg(client, args.memory_namespace, fixture, facts, hyp)
    return row


# ------------------------------------------------------------------ report


def _rate(hits: int, n: int) -> str:
    return f"{hits}/{n}" + (f" ({100.0 * hits / n:.0f}%)" if n else "")


def _agree(expected: bool | None, got: bool | None) -> bool | None:
    return None if expected is None or got is None else expected == got


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Agreement rates over the replayed fixtures; None-valued verdicts
    (loom could not say) are counted apart from disagreements."""
    out: dict[str, Any] = {"fixtures": len(rows)}
    for anchor in ("captured", "merge"):
        pairs = [_agree(r["expected_g1"], (r["g1"].get(anchor) or {}).get("loom_g1")) for r in rows]
        known = [p for p in pairs if p is not None]
        out[f"g1_{anchor}"] = {
            "agree": sum(known),
            "decided": len(known),
            "undecided": len(pairs) - len(known),
            "commit_known": sum(1 for r in rows if (r["g1"].get(anchor) or {}).get("commit_known")),
        }
    loc_n = sum(r["impact"]["n"] for r in rows)
    out["impact"] = {
        "located": sum(r["impact"]["located"] for r in rows),
        "locations": loc_n,
        "fixtures_all_located": sum(
            1 for r in rows if r["impact"]["n"] and r["impact"]["located"] == r["impact"]["n"]
        ),
        "verdicts": Counter(str(r["impact"]["verdict"]) for r in rows),
    }
    cov = [c for r in rows for c in r["coverage"]]
    # A fixture's G4 is decided on its coverage calls that answered.
    answered = {id(r): [c for c in r["coverage"] if not c.get("error")] for r in rows}
    out["coverage"] = {
        "subjects": len(cov),
        "errored": sum(1 for c in cov if c.get("error")),
        "measurable": sum(1 for c in cov if c["measurable"]),
        "verdicts": Counter(str(c["verdict"]) for c in cov),
        "g4_agree": sum(
            1
            for r in rows
            if answered[id(r)]
            and _agree(r["expected_g4"], any(c["measurable"] for c in answered[id(r)]))
        ),
        "g4_decided": sum(1 for r in rows if answered[id(r)] and r["expected_g4"] is not None),
    }
    all_neg = [r["negative"] for r in rows if r.get("negative")]
    # A failed search surfaces nothing, so it must not count as a negative-gate pass.
    neg = [n for n in all_neg if not n.get("error")]
    out["negative"] = {
        "fixtures": len(neg),
        "errored": len(all_neg) - len(neg),
        "not_surfaced": sum(1 for n in neg if n["rank"] is None),
        "control_surfaced": sum(1 for n in neg if n["control_rank"] is not None),
        "not_surfaced_and_control_surfaced": sum(
            1 for n in neg if n["rank"] is None and n["control_rank"] is not None
        ),
    }
    return out


def _mark(value: bool | None) -> str:
    return "?" if value is None else ("T" if value else "F")


def _g1_cell(g1: dict[str, Any]) -> str:
    mark = _mark(g1.get("loom_g1"))
    return mark if g1.get("commit_known") or not g1.get("sha") else f"{mark}*"


def markdown_table(rows: list[dict[str, Any]]) -> str:
    head = (
        "| fixture | PR | category | G1 exp | G1@captured | G1@merge | impact located "
        "| impact verdict | coverage (verdicts) | G4 exp / measurable "
        "| negative rank / control rank |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    lines = [head]
    for r in rows:
        cov = ",".join(str(c["verdict"]) for c in r["coverage"]) or "-"
        meas = "T" if any(c["measurable"] for c in r["coverage"]) else "F"
        neg = r.get("negative")
        neg_cell = f"{_rank(neg['rank'])} / {_rank(neg['control_rank'])}" if neg else ""
        imp = r["impact"]
        lines.append(
            f"| {r['fixture_id']} | {r['pr']} | {r['outcome_category']} "
            f"| {_mark(r['expected_g1'])} "
            f"| {_g1_cell(r['g1'].get('captured') or {})} "
            f"| {_g1_cell(r['g1'].get('merge') or {})} "
            f"| {imp['located']}/{imp['n']} "
            f"| {imp['verdict'] or str(imp.get('impacted', 0)) + ' impacted'} | {cov} "
            f"| {_mark(r['expected_g4'])} / {meas} | {neg_cell} |"
        )
    return "\n".join(lines)


def _rank(rank: int | None) -> str:
    return "-" if rank is None else str(rank)


def markdown_summary(summary: dict[str, Any], client: Client | None = None) -> str:
    g1c, g1m, imp, cov, neg = (
        summary["g1_captured"],
        summary["g1_merge"],
        summary["impact"],
        summary["coverage"],
        summary["negative"],
    )
    lines = [
        f"fixtures: {summary['fixtures']}",
        f"G1 agreement @captured_sha: {_rate(g1c['agree'], g1c['decided'])}, "
        f"undecided {g1c['undecided']}, commit known {g1c['commit_known']}",
        f"G1 agreement @merge_sha: {_rate(g1m['agree'], g1m['decided'])}, "
        f"undecided {g1m['undecided']}, commit known {g1m['commit_known']}",
        f"impact: locations with a touched unit {_rate(imp['located'], imp['locations'])}; "
        f"fixtures fully located {imp['fixtures_all_located']}; verdicts {dict(imp['verdicts'])}",
        f"coverage: measurable subjects {_rate(cov['measurable'], cov['subjects'])} "
        f"(errored {cov['errored']}); "
        f"verdicts {dict(cov['verdicts'])}; "
        f"G4 agreement {_rate(cov['g4_agree'], cov['g4_decided'])}",
        f"negative gate: not surfaced {_rate(neg['not_surfaced'], neg['fixtures'])}; "
        f"positive control surfaced {_rate(neg['control_surfaced'], neg['fixtures'])}; "
        f"both {neg['not_surfaced_and_control_surfaced']}; errored {neg['errored']}",
    ]
    if client is not None:
        if client.errors:
            lines.append(f"errored calls: {dict(client.errors)}")
        for op, ms in sorted(client.ms.items()):
            q = statistics.quantiles(ms, n=10) if len(ms) >= 2 else [ms[0]] * 9
            lines.append(f"{op}: n={len(ms)} p50={statistics.median(ms):.0f} ms p90={q[8]:.0f} ms")
    return "\n".join(lines)


# ------------------------------------------------------------------ main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base", default=os.environ.get("LOOM_BASE_URL", "http://127.0.0.1:7777"))
    p.add_argument("--token-file", default=os.environ.get("LOOM_API_KEY_FILE", ""))
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--fixtures", default=FIXTURES_DIR)
    p.add_argument("--org", default="clickhouse")
    p.add_argument("--namespace", default="code-clickhouse")
    p.add_argument("--tracker-namespace", default="tracker-clickhouse")
    p.add_argument("--memory-namespace", default="clickgap-public")
    p.add_argument("--consumer", default="bench-clickgap-golden")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--limit", type=int, default=0, help="replay only the first N fixtures")
    p.add_argument(
        "--skip-memory", action="store_true", help="skip the memory.search negative gate"
    )
    p.add_argument("--out", default="", help="per-fixture JSONL")
    p.add_argument("--md", default="", help="markdown table + summary")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], Client]:
    token = args.token or (open(args.token_file).read().strip() if args.token_file else "")
    if not token:
        sys.exit("a bearer token is required (--token / --token-file / LOOM_API_KEY)")
    fixtures = load_fixtures(args.fixtures)
    if args.limit:
        fixtures = fixtures[: args.limit]
    client = Client(
        args.base, token, consumer=args.consumer, org=args.org, concurrency=args.concurrency
    )
    try:
        rows = await asyncio.gather(*(replay_fixture(client, f, args) for f in fixtures))
    finally:
        await client.aclose()
    return list(rows), summarize(list(rows)), client


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows, summary, client = asyncio.run(run(args))
    table, text = markdown_table(rows), markdown_summary(summary, client)
    print(table)
    print()
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=str) + "\n")
    if args.md:
        with open(args.md, "w") as fh:
            fh.write(table + "\n\n" + text + "\n")
    return 1 if client.errors else 0


if __name__ == "__main__":
    sys.exit(main())
