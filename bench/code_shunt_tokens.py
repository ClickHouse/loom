"""Token cost of the bulk-read reflex vs asking the index — the measurement
behind loom-code-shunt (integrations/claude-code-plugin/hooks/loom-code-shunt.mjs).

For N recent merged PRs: the "cold" agent reads every changed file in full (what a
Read/`cat` ingests); the "shunted" agent takes the PR's context pack plus one index call
per changed file — the redirect the hook proposes. Two arms select that call:
`auto-search` (default; code.search path-scoped, top_k=3, the older redirect) and
`--outline` (one code.outline per file: declarations with line spans, whole-row budgeted
— the redirect the hook names). Tokens: a real tokenizer (o200k_base via tiktoken) when
installed, else the measured chars-per-token rates of server.code.domain.units; the
server's own tokens_returned is the price on the index side. The outline arm also
reports code.outline latency percentiles (p50/p95 of the response `took_ms` and of the
wall clock) and the entry / elided counts per file.

  python bench/code_shunt_tokens.py --base-url http://localhost:7777 \
      --token $LOOM_API_KEY --git-dir var/code_repos/clickhouse.git --prs 30
  python bench/code_shunt_tokens.py ... --outline        # the outline arm

`--post` additionally records each PR's hook-faithful saving as a
`code.outcome` row (action=shunt_hit, tokens_saved=cold - hook, source=
bench-shunt-tokens, targets=["bench:pr:<n>"]) so the token-saving number is a
queryable series in loom.code_search_outcome next to the plugin's live counts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

try:  # A real tokenizer when one is installed
    import tiktoken

    _ENC = tiktoken.get_encoding("o200k_base")
except Exception:  # noqa: BLE001 — optional dependency
    _ENC = None


def count_tokens(text: bytes | str, path: str = "") -> int:
    """Tokens the model ingests for `text`: o200k_base when tiktoken is
    present, else the per-kind chars/token rates measured
    per kind (never bytes/4 for sql/sh/xml/yaml, where that under-counts)."""
    s = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    if _ENC is not None:
        return len(_ENC.encode(s, disallowed_special=()))
    try:
        from server.code.domain.units import estimate_tokens

        return estimate_tokens(s, path)
    except Exception:  # noqa: BLE001 — bench runs without the server tree too
        return len(s) // 4


def tokenizer_name() -> str:
    return "o200k_base" if _ENC is not None else "code_units.estimate_tokens (per-kind rates)"


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(round(q * (len(ys) - 1))))]


@dataclass
class Row:
    pr: int
    sha: str
    files: int = 0
    cold_tokens: int = 0
    pack_tokens: int = 0
    search_tokens: int = 0
    follow_ups: int = 0
    # Hook-faithful variant: files at or under the line threshold are read in
    # full (the hook lets them through); only the big ones are shunted.
    small_file_tokens: int = 0
    big_search_tokens: int = 0
    big_files: int = 0
    # outline arm only: per-file (took_ms, wall_ms, total, returned, indexed)
    outline_calls: list[dict[str, int]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def shunt_tokens(self) -> int:
        return self.pack_tokens + self.search_tokens

    @property
    def hook_tokens(self) -> int:
        return self.pack_tokens + self.small_file_tokens + self.big_search_tokens


def _git(git_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", git_dir, *args],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout


def changed_files(git_dir: str, sha: str, prefixes: list[str]) -> list[str]:
    # --diff-filter=d: a file the PR deleted has no blob at `sha` to read cold
    out = _git(git_dir, "diff", "--name-only", "--diff-filter=d", f"{sha}^1", sha)
    files = [p for p in out.split("\n") if p.strip()]
    return [p for p in files if any(p.startswith(pre) for pre in prefixes)]


def file_blob(git_dir: str, sha: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "--git-dir", git_dir, "show", f"{sha}:{path}"],
        capture_output=True,
        timeout=60,
        check=True,
    ).stdout


def shunt_outcome_body(r: Row) -> dict[str, object]:
    """The code.outcome payload for one measured PR: the hook-faithful saving
    (files over the line threshold shunted, the rest read in full), never
    negative — a PR where the index cost more records 0 saved, not a debt."""
    return {
        "action": "shunt_hit",
        "trace_run_id": "",
        "targets": [f"bench:pr:{r.pr}"],
        "tokens_saved": max(0, r.cold_tokens - r.hook_tokens),
        "source": "bench-shunt-tokens",
        "consumer": "bench-shunt",
    }


async def _op(c: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """One op call. A non-2xx (a 429, an expired token) raises instead of reading
    as zero tokens returned, which would make the index side look cheaper."""
    resp = await c.post(path, json=body)
    resp.raise_for_status()
    return resp.json()


async def measure_pr(
    c: httpx.AsyncClient, r: Row, files: list[str], args: argparse.Namespace
) -> None:
    """Fill `r` with the cold read of `files` and the shunted arm's index cost."""
    blobs = {f: file_blob(args.git_dir, r.sha, f) for f in files}
    r.cold_tokens = sum(count_tokens(b, f) for f, b in blobs.items())
    pack = await _op(c, "/v1/code.context_pack", {"pr_number": r.pr, "token_budget": 2500})
    r.pack_tokens = int(pack.get("tokens_used") or 0)
    for f in files:
        if args.outline:
            t0 = time.monotonic()
            res = await _op(
                c,
                "/v1/code.outline",
                {"path": f, "token_budget": args.outline_budget, "consumer": "bench-shunt"},
            )
            r.outline_calls.append(
                {
                    "took_ms": int(res.get("took_ms") or 0),
                    "wall_ms": int((time.monotonic() - t0) * 1000),
                    "total": int(res.get("total") or 0),
                    "returned": int(res.get("returned") or 0),
                    "indexed": int(bool(res.get("indexed"))),
                    "lines": blobs[f].count(b"\n"),
                }
            )
        else:
            res = await _op(
                c,
                "/v1/code.search",
                {
                    "query": f,
                    "path_prefix": f,
                    "top_k": args.top_k,
                    "bodies": args.bodies,
                    "consumer": "bench-shunt",
                },
            )
        got = int(res.get("tokens_returned") or 0)
        if args.bodies == "none" and res.get("results"):
            # The on-demand shape: the agent reads the ONE body it wants.
            top = res["results"][0]
            enc = await _op(
                c,
                "/v1/code.enclosing",
                {
                    "locations": [f"{top['path']}:{int(top.get('start_line') or 1)}"],
                    "include_body": True,
                    "consumer": "bench-shunt",
                },
            )
            body = "".join(
                str(e.get("body") or "") for e in enc.get("results") or enc.get("entries") or []
            )
            got += count_tokens(body, top["path"])
            r.follow_ups += 1
        r.search_tokens += got
        if blobs[f].count(b"\n") > args.threshold_lines:
            r.big_files += 1
            r.big_search_tokens += got
        else:
            r.small_file_tokens += count_tokens(blobs[f], f)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", ""))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--git-dir", required=True)
    ap.add_argument("--prs", type=int, default=30)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument(
        "--bodies",
        choices=["auto", "none"],
        default="auto",
        help="code.search bodies mode for the shunted agent: "
        "'none' serves handles and adds ONE code.enclosing body fetch for the "
        "top hit per file, the on-demand shape",
    )
    ap.add_argument(
        "--threshold-lines",
        type=int,
        default=350,
        help="hook threshold: files with more lines are shunted, the rest read in full",
    )
    ap.add_argument("--json", action="store_true", help="emit per-PR rows as JSON")
    ap.add_argument(
        "--post",
        action="store_true",
        help="record each PR's measured saving as a shunt_hit code.outcome row",
    )
    ap.add_argument(
        "--outline",
        action="store_true",
        help="arm: one code.outline per changed file instead of a path-scoped code.search",
    )
    ap.add_argument(
        "--outline-budget",
        type=int,
        default=1500,
        help="code.outline token_budget for the outline arm",
    )
    args = ap.parse_args(argv)
    if not args.base_url:
        print("--base-url (or LOOM_BASE_URL) is required", file=sys.stderr)
        return 2
    arm = "outline" if args.outline else "auto-search"

    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=30) as c:
        st = await _op(c, "/v1/code.index_status", {})
        prefixes = st.get("path_prefixes") or ["src/"]
        packs = (await _op(c, "/v1/code.context_packs", {"limit": args.prs}))["rows"]
        rows: list[Row] = []
        for p in packs:
            r = Row(pr=int(p["pr_number"]), sha=str(p["merge_commit_sha"]))
            try:
                files = changed_files(args.git_dir, r.sha, prefixes)
            except Exception as e:  # noqa: BLE001
                r.errors.append(f"git: {e}")
                files = []
            r.files = len(files)
            if not files:
                r.errors.append("no indexed files changed")
                rows.append(r)
                continue
            try:
                await measure_pr(c, r, files, args)
            except (httpx.HTTPError, subprocess.SubprocessError, ValueError) as e:
                r.errors.append(f"measure: {e!r}"[:200])
            rows.append(r)

        ok = [r for r in rows if not r.errors]
        if args.post and ok:
            posted = 0
            for r in ok:
                try:
                    resp = await c.post("/v1/code.outcome", json=shunt_outcome_body(r))
                    posted += int(resp.status_code < 300)
                except Exception as e:  # noqa: BLE001 — accounting is best-effort
                    print(f"post failed for PR {r.pr}: {e}", file=sys.stderr)
            print(f"posted {posted}/{len(ok)} shunt_hit outcome rows", file=sys.stderr)

    ok = [r for r in ok if r.cold_tokens]  # a ratio needs a cold read to divide by
    if not ok:
        print("no measurable PRs", file=sys.stderr)
        return 1
    savings = [1 - (r.shunt_tokens / r.cold_tokens) for r in ok]
    print(f"\n=== code_shunt_tokens — arm {arm}, tokens by {tokenizer_name()} ===")
    skipped: dict[str, int] = {}
    for r in rows:
        if r.errors:
            reason = r.errors[0].split(":")[0]
            skipped[reason] = skipped.get(reason, 0) + 1
    print(f"PRs measured: {len(ok)} (skipped {len(rows) - len(ok)}: {skipped or 'none'})")
    print(f"changed files/PR (mean): {statistics.mean(r.files for r in ok):.1f}")
    cold = [r.cold_tokens for r in ok]
    shunt = [r.shunt_tokens for r in ok]
    print(
        f"cold whole-file read tokens/PR:    mean {statistics.mean(cold):>8.0f}   "
        f"median {statistics.median(cold):>8.0f}"
    )
    print(
        f"shunted (pack + {arm}) tokens/PR: mean {statistics.mean(shunt):>8.0f}   "
        f"median {statistics.median(shunt):>8.0f}"
    )
    print(
        f"bodies={args.bodies}; follow-up code.enclosing calls/PR (mean): "
        f"{statistics.mean(r.follow_ups for r in ok):.1f}"
    )
    print(
        f"savings: mean {statistics.mean(savings):.1%}   median {statistics.median(savings):.1%}"
        f"   min {min(savings):.1%}   max {max(savings):.1%}"
    )
    worse = [r for r in ok if r.shunt_tokens >= r.cold_tokens]
    print(
        f"PRs where the index cost MORE than reading: {len(worse)} "
        f"(small diffs in small files — the hook's line threshold passes those through)"
    )
    hook = [r.hook_tokens for r in ok]
    hook_sav = [1 - (r.hook_tokens / r.cold_tokens) for r in ok]
    big = sum(r.big_files for r in ok)
    print(
        f"--- hook-faithful (files > {args.threshold_lines} lines shunted, {big} of "
        f"{sum(r.files for r in ok)} files; the rest read in full) ---"
    )
    print(f"tokens/PR: mean {statistics.mean(hook):>8.0f}   median {statistics.median(hook):>8.0f}")
    hs_mean, hs_med = statistics.mean(hook_sav), statistics.median(hook_sav)
    print(f"savings:   mean {hs_mean:.1%}   median {hs_med:.1%}   min {min(hook_sav):.1%}")
    calls = [c for r in ok for c in r.outline_calls]
    if calls:
        took = [float(c["took_ms"]) for c in calls]
        wall = [float(c["wall_ms"]) for c in calls]
        idx = [c for c in calls if c["indexed"]]
        lines = sum(c["lines"] for c in idx) or 1
        per_file = [r.search_tokens / max(1, len(r.outline_calls)) for r in ok]
        print(f"--- code.outline: {len(calls)} calls, {len(idx)} on indexed paths ---")
        print(
            f"took_ms (server): p50 {pct(took, 0.5):.0f}   p95 {pct(took, 0.95):.0f}   "
            f"wall_ms: p50 {pct(wall, 0.5):.0f}   p95 {pct(wall, 0.95):.0f}"
        )
        tot = statistics.mean(c["total"] for c in idx) if idx else 0
        ret = statistics.mean(c["returned"] for c in idx) if idx else 0
        print(
            f"entries/file: mean total {tot:.1f}   returned {ret:.1f}"
            f"   files with elision {sum(c['returned'] < c['total'] for c in idx)}"
        )
        print(
            f"outline tokens/file (mean) {statistics.mean(per_file):.0f}; "
            f"tokens per file line {sum(r.search_tokens for r in ok) / lines:.3f}"
        )
    if args.json:
        print(json.dumps([r.__dict__ for r in rows], default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
