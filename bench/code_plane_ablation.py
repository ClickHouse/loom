"""Agent-level ablation of the code plane: the same coding-agent harness with
the plugin toggled, judged by what the agent actually read.

The retrieval benches (bench/code_intel_qa.py and its mined lanes) score the
index. This bench scores the AGENT with the index attached — the question a
deployment asks. Mechanisms:

  same-harness toggle   one `claude -p` harness, arms differ only in the
                        plugin dir and MCP config (bench/agent_traj/)
  repetitions           `--seeds N` independent repeats per (task, arm);
                        the model is not seedable, so a seed is a repeat
  paired statistics     per-task means over seeds, paired Wilcoxon
                        signed-rank between arms, paired bootstrap lower
                        bounds for the gate deltas
  leak audit            a public exclusion ledger (exclusions.csv): cells
                        that fetched the network, read git history past the
                        base commit, or whose task text still named a gold
                        stem after redaction are excluded, fail-closed
  View B localization   from tool-call provenance: a path the agent only saw
                        inside a loom code.* result is NOT credited; only
                        paths the agent itself read, grepped or listed count
  $/solved              cost per task whose View-B top-5 hit gold
  wasted-read precision file / line (and, resolved, symbol) precision of
                        the agent's actual Read spans, three arms including
                        a read-window cap

Tasks are merged PRs of the indexed repository: task text = title + body
with gold path stems and changed-symbol identifiers redacted; tasks whose
text still names a gold stem are dropped and the share reported. Gold =
the PR's changed src/ files (with the diff's old-side hunks as line spans)
plus their 1-hop callers at edge confidence >= 0.8. The agent runs in a
detached worktree of the bare mirror at the PR's BASE commit — it sees the
tree before the fix — with turn and dollar budgets.

Honesty flag every task carries: `fix_in_index`, true when the PR's merge
commit is an ancestor of the index head the arms read. On a live namespace
that is every task (the index is at the branch tip), and the numbers are
an upper bound; a namespace indexed at a pinned snapshot flips it.

    # mine 12 tasks from the mirror (bodies via `gh api` when available)
    python bench/code_plane_ablation.py mine --repo var/code_repos/clickhouse.git \
        --index-head <sha> --namespace code-clickhouse --n 12 --out runs/pilot/tasks.jsonl

    # dry run: compose every cell's argv/env, spend nothing
    python bench/code_plane_ablation.py run --tasks runs/pilot/tasks.jsonl --arms A,B,C \
        --seeds 1 --driver dry --out runs/pilot --plugin-src integrations/claude-code-plugin

    # score (transcripts already in runs/pilot/cells/)
    python bench/code_plane_ablation.py score --tasks runs/pilot/tasks.jsonl --runs runs/pilot

    # scorer self-test on the synthetic transcript fixture
    python bench/code_plane_ablation.py selftest
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import os
import random
import re
import shlex
import statistics
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURE_DIR = HERE / "fixtures" / "code_plane_ablation"
DEFAULT_PLUGIN_SRC = HERE.parent / "integrations" / "claude-code-plugin"

GOLD_PREFIX = "src/"
DEFAULT_READ_LIMIT = 2000  # Claude Code's Read without `limit`
# Repository-relative paths in tool output, for the ClickHouse tree's top-level roots.
PATH_RE = re.compile(r"(?<![\w/.-])((?:src|base|programs|tests|ci)/[\w./+-]+\.[A-Za-z0-9]+)")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)(?:\t|→| )", re.M)
GREP_HIT_RE = re.compile(r"^([^\s:]+?):(\d+)[:-]", re.M)
PR_NUMBER_RE = re.compile(r"Merge pull request #(\d+)")
# A squash-merged PR names itself at the end of its subject.
SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$", re.M)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
NETWORK_RE = re.compile(r"\b(curl|wget|gh|pip|npm|git\s+(fetch|pull|clone|ls-remote))\b")
BYPASS_RE = re.compile(r"LOOM_CODE_SHUNT\s*=\s*off", re.I)
HISTORY_RE = re.compile(r"\bgit\s+(log|show|diff|blame|reflog|rev-list|describe|whatchanged)\b")
LOOM_TOOL_RE = re.compile(r"^mcp__[^_]+(?:_[^_]+)*__loom_code_|^mcp__.*__code_", re.I)


# ===========================================================================
# tasks: mining, redaction, leak filter
# ===========================================================================


@dataclass
class Task:
    id: str
    pr_number: int
    title: str
    body: str  # redacted
    prompt: str
    base_sha: str
    merge_sha: str
    index_head: str
    namespace: str
    fix_in_index: bool
    gold_files: list[str]
    gold_callers: list[str]
    gold_spans: dict[str, list[list[int]]]  # path -> [[start, end], ...] old side
    redacted_terms: list[str]
    lane: str = "pr_title"
    leak_dropped: bool = False
    leak_reasons: list[str] = field(default_factory=list)

    @property
    def gold_ext(self) -> set[str]:
        return set(self.gold_files) | set(self.gold_callers)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=600
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def stem_of(path: str) -> str:
    return path.rsplit("/", 1)[-1].split(".", 1)[0]


def strip_pr_template(body: str) -> str:
    """Drop the PR template's HTML comments, checkbox boilerplate and the
    changelog/documentation prompts; keep what the author wrote."""
    text = HTML_COMMENT_RE.sub("", body or "")
    keep: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            keep.append("")
            continue
        low = s.lower()
        if low.startswith(("changelog category", "changelog entry", "documentation entry")):
            continue
        if low.startswith(("- [ ]", "- [x]", "* [ ]", "* [x]")):
            continue
        if low.startswith("#### ") and "changelog" in low:
            continue
        keep.append(s)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    return out


def hunk_identifiers(diff_text: str, min_len: int = 5) -> set[str]:
    """Identifiers from hunk-header context and changed lines: CamelCase or
    snake_case names of `min_len`+ characters, the symbols a PR touches."""
    out: set[str] = set()
    for m in HUNK_RE.finditer(diff_text):
        for ident in IDENT_RE.findall(m.group(5) or ""):
            if _symbol_like(ident, min_len):
                out.add(ident)
    for ln in diff_text.splitlines():
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")):
            for ident in IDENT_RE.findall(ln[1:]):
                if _symbol_like(ident, min_len + 1):
                    out.add(ident)
    return out


def _symbol_like(ident: str, min_len: int) -> bool:
    if len(ident) < min_len or ident.isupper() and "_" not in ident:
        return False
    if ident.lower() in _COMMON_WORDS:
        return False
    has_case = any(c.isupper() for c in ident[1:]) and any(c.islower() for c in ident)
    return has_case or "_" in ident


_COMMON_WORDS = frozenset(
    {
        "return",
        "const",
        "static",
        "struct",
        "class",
        "public",
        "private",
        "template",
        "typename",
        "namespace",
        "include",
        "define",
        "override",
        "virtual",
        "explicit",
        "nullptr",
        "string",
        "vector",
        "size_t",
        "uint64_t",
        "int64_t",
        "uint32_t",
        "int32_t",
        "uint8_t",
        "bool",
        "auto",
        "void",
        "throw",
        "catch",
        "while",
        "break",
        "continue",
        "default",
        "switch",
        "false",
        "true",
        "sizeof",
        "operator",
        "delete",
        "inline",
        "friend",
        "mutable",
        "constexpr",
        "noexcept",
        "chassert",
        "context",
        "settings",
        "column",
        "columns",
        "block",
        "result",
        "value",
        "values",
        "query",
    }
)


def redact(text: str, terms: Iterable[str]) -> tuple[str, list[str]]:
    """Replace every occurrence of each term (case-insensitive, identifier
    boundaries) with [redacted]; returns the text and the terms that hit."""
    hit: list[str] = []
    out = text
    for t in sorted(set(terms), key=len, reverse=True):
        if len(t) < 4:
            continue
        pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", re.I)
        if pat.search(out):
            hit.append(t)
            out = pat.sub("[redacted]", out)
    return out, sorted(hit)


def leak_reasons_for(text: str, gold_files: Iterable[str], min_stem: int = 4) -> list[str]:
    """A gold stem surviving in the task text as a substring of any word is a
    leak the agent could grep out of the prompt. Fail closed: substring, not
    whole-word, case-insensitive."""
    low = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", low))
    reasons: list[str] = []
    for f in gold_files:
        s = stem_of(f).lower()
        if len(s) < min_stem:
            continue
        if any(s in w for w in words):
            reasons.append(f"stem:{s}")
    return sorted(set(reasons))


def build_prompt(title: str, body: str, base_sha: str) -> str:
    desc = body.strip() or "(no description beyond the title)"
    return (
        "You are working in a read-only checkout of the ClickHouse repository at commit "
        f"{base_sha[:12]}. A change request follows; identifiers that would name the answer "
        "are shown as [redacted].\n\n"
        f"Title: {title}\n\nDescription:\n{desc}\n\n"
        "Localize where this change must be made: explore the tree, then name the src/ "
        "files that must be modified to implement it. Rules: do not edit files; do not use "
        "the network; do not read git history, diffs or branches (the change is not in this "
        "checkout). Keep exploration proportionate — stop when you are confident.\n\n"
        "Finish with exactly one line of the form\n"
        "FILES: path/one.cpp, path/two.h\n"
        "listing repo-relative paths, most confident first (at most 8)."
    )


def build_system_prompt(arm: str, namespace: str, index_head: str, org: str = "dev") -> str:
    if arm in ("C", "D", "E", "F"):
        return (
            "Loom code-index tools for this repository are available as MCP tools "
            "(loom_code_search, loom_code_symbol, loom_code_callers, loom_code_outline, "
            f"loom_code_ask). Pass org='{org}' and namespace='{namespace}' in every call. "
            f"The index describes the tree as of commit {index_head[:12]}. When a Read or "
            "Grep is denied with a redirect, follow the redirect."
        )
    return ""


def parse_hunks(diff_text: str) -> dict[str, list[list[int]]]:
    """Old-side line spans per path from a `-U0` diff; a pure insertion (old
    count 0) becomes a one-line anchor at the insertion point."""
    spans: dict[str, list[list[int]]] = {}
    cur: str | None = None
    for ln in diff_text.splitlines():
        if ln.startswith("--- "):
            cur = None
            p = ln[4:].strip()
            if p.startswith("a/"):
                cur = p[2:]
            elif p != "/dev/null":
                cur = p
            continue
        if ln.startswith("+++ ") and cur is None:
            p = ln[4:].strip()
            if p.startswith("b/"):
                cur = p[2:]
            continue
        m = HUNK_RE.match(ln)
        if m and cur:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            end = start + max(count, 1) - 1
            spans.setdefault(cur, []).append([max(start, 1), max(end, 1)])
    return spans


def mine_tasks(
    repo: Path,
    index_head: str,
    namespace: str,
    n: int,
    *,
    ref: str = "HEAD",
    min_files: int = 1,
    max_files: int = 8,
    scan: int = 400,
    fetch_body: Callable[[int], dict[str, Any] | None] | None = None,
    callers: Callable[[list[str]], list[str]] | None = None,
    min_body_chars: int = 0,
    squash: bool = False,
    merged_prs: list[tuple[str, int]] | None = None,
    gold_prefix: str = GOLD_PREFIX,
) -> tuple[list[Task], dict[str, int]]:
    """Walk first-parent merge commits from `ref`; keep PRs with 1..8 changed
    src/ files, not reverts, not doc regeneration. Every filter is counted.
    `squash`: a repository that squash-merges has no merge
    commits — every first-parent commit whose subject carries `(#N)` is the
    PR, its single parent the base (such a repository shows 0 merges without it)."""
    if merged_prs:
        # The PR list came from GitHub (merge_commit_sha per
        # merged PR); the record is rebuilt from the mirror so the rest of the
        # walk is byte for byte the log path
        recs = []
        for sha, pr in merged_prs:
            try:
                rec = _git(repo, "log", "-1", "--format=%H|%P|%s|%b", sha)
            except Exception:  # noqa: BLE001 — a PR whose merge is not in the mirror
                continue
            head_, _, rest_ = rec.partition("|")
            parents_, _, rest_ = rest_.partition("|")
            subject_, _, body_ = rest_.partition("|")
            recs.append(f"{head_}|{parents_}|Merge pull request #{pr} {subject_}|{body_}")
        log = "\x00".join(recs)
    else:
        log = _git(
            repo,
            "log",
            ref,
            *([] if squash else ["--merges"]),
            "--first-parent",
            f"-n{scan}",
            "--format=%H|%P|%s|%b%x00",
        )
    stats = {
        "scanned": 0,
        "no_pr_number": 0,
        "file_count_out_of_range": 0,
        "revert_or_autogen": 0,
        "short_body": 0,
        "leak_dropped": 0,
        "kept": 0,
    }
    tasks: list[Task] = []
    for rec in log.split("\x00"):
        rec = rec.strip()
        if not rec:
            continue
        stats["scanned"] += 1
        head, _, rest = rec.partition("|")
        parents, _, rest = rest.partition("|")
        subject, _, body_lines = rest.partition("|")
        m = PR_NUMBER_RE.search(subject) or (SQUASH_PR_RE.search(subject) if squash else None)
        if not m:
            stats["no_pr_number"] += 1
            continue
        pr = int(m.group(1))
        base = parents.split()[0]
        title = (body_lines.strip().splitlines() or [subject])[0].strip()
        low = title.lower()
        if low.startswith("revert") or "docs: regenerate" in low or low.startswith("backport"):
            stats["revert_or_autogen"] += 1
            continue
        files = [
            f
            for f in _git(
                repo, "diff-tree", "--no-commit-id", "-r", "--name-only", base, head
            ).split()
            if f.startswith(gold_prefix)
        ]
        if not (min_files <= len(files) <= max_files):
            stats["file_count_out_of_range"] += 1
            continue
        body = ""
        lane = "pr_title"
        if fetch_body is not None:
            meta = fetch_body(pr) or {}
            body = strip_pr_template(str(meta.get("body") or ""))
            title = str(meta.get("title") or title).strip()
            if body:
                lane = "pr_body"
        if len(body) < min_body_chars:
            stats["short_body"] += 1
            continue
        diff = _git(repo, "diff", "-U0", base, head, "--", *files)
        spans = parse_hunks(diff)
        terms = {stem_of(f) for f in files} | hunk_identifiers(diff)
        text = f"{title}\n{body}"
        red_text, hits = redact(text, terms)
        red_title, red_body = red_text.split("\n", 1) if "\n" in red_text else (red_text, "")
        reasons = leak_reasons_for(red_text, files)
        fix_in_index = _is_ancestor(repo, head, index_head)
        t = Task(
            id=f"pr{pr}",
            pr_number=pr,
            title=red_title,
            body=red_body,
            prompt=build_prompt(red_title, red_body, base),
            base_sha=base,
            merge_sha=head,
            index_head=index_head,
            namespace=namespace,
            fix_in_index=fix_in_index,
            gold_files=sorted(files),
            gold_callers=sorted(set(callers(files)) - set(files)) if callers else [],
            gold_spans=spans,
            redacted_terms=hits,
            lane=lane,
            leak_dropped=bool(reasons),
            leak_reasons=reasons,
        )
        if reasons:
            stats["leak_dropped"] += 1
            tasks.append(t)  # kept in the ledger, never run
            continue
        stats["kept"] += 1
        tasks.append(t)
        if stats["kept"] >= n:
            break
    return tasks, stats


def _is_ancestor(repo: Path, commit: str, head: str) -> bool:
    if not head:
        return False
    r = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, head],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return r.returncode == 0


def gh_pr_fetcher(owner_repo: str) -> Callable[[int], dict[str, Any] | None]:
    def fetch(pr: int) -> dict[str, Any] | None:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}/pulls/{pr}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if r.returncode != 0:
            return None
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
        return {"title": d.get("title"), "body": d.get("body"), "merged_at": d.get("merged_at")}

    return fetch


def write_tasks(tasks: Sequence[Task], stats: dict[str, int], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        fh.write(json.dumps({"_header": True, "stats": stats}) + "\n")
        for t in tasks:
            fh.write(json.dumps(asdict(t)) + "\n")


def load_tasks(path: Path) -> tuple[list[Task], dict[str, int]]:
    tasks: list[Task] = []
    stats: dict[str, int] = {}
    for ln in path.read_text().splitlines():
        if not ln.strip():
            continue
        d = json.loads(ln)
        if d.get("_header"):
            stats = d.get("stats", {})
            continue
        tasks.append(Task(**d))
    return tasks, stats


# ===========================================================================
# transcript -> events -> spans
# ===========================================================================


@dataclass
class ReadEvent:
    order: int
    tool: str  # Read | Grep | Bash | Edit | loom | other
    path: str | None  # repo-relative, None when outside the repo
    start: int
    end: int  # 0,0 = file-level provenance only
    denied: bool
    provenance: str  # agent | loom
    result_bytes: int
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Transcript:
    events: list[ReadEvent]
    loom_paths: list[str]  # paths first seen inside loom tool results, in order
    final_text: str
    answered_files: list[str]
    tool_calls: int
    loom_calls: int
    denied_calls: int
    bypass_calls: int  # Bash commands that set LOOM_CODE_SHUNT=off (the deny text names it)
    cost_usd: float
    tokens_in: int
    tokens_out: int
    tokens_cache: int
    num_turns: int
    plugins: list[str]
    mcp_servers: list[str]
    flags: list[str]  # audit: network | git_history | web_tool | edit
    result_seen: bool
    api_key_source: str = ""  # init row apiKeySource: ANTHROPIC_API_KEY | none | ...


def rel_path(p: str, workdir: str | None) -> str | None:
    """Repo-relative POSIX path, or None when it is outside the checkout."""
    if not p:
        return None
    p = p.replace("\\", "/")
    if workdir:
        w = workdir.rstrip("/") + "/"
        if p.startswith(w):
            return p[len(w) :]
        if p == workdir.rstrip("/"):
            return ""
    if p.startswith("/"):
        m = re.search(r"/((?:src|base|programs|tests|ci)/.*)$", p)
        return m.group(1) if m else None
    return p.lstrip("./") or ""


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(str(c.get("text", "")))
                elif "text" in c:
                    parts.append(str(c["text"]))
            else:
                parts.append(str(c))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content)
    return str(content or "")


def _span_from_numbered(result: str, offset: int | None, limit: int | None) -> tuple[int, int]:
    nums = [int(x) for x in NUMBERED_LINE_RE.findall(result)]
    if offset is not None or limit is not None:
        start = max(1, int(offset or 1))
        want = int(limit) if limit else DEFAULT_READ_LIMIT
        end = start + want - 1
        if nums:
            end = min(end, max(nums))
        return start, max(start, end)
    if nums:
        return min(nums), max(nums)
    return 1, max(1, result.count("\n"))


def parse_bash_reads(command: str) -> list[dict[str, Any]]:
    """Read-shaped Bash commands -> [{path, start, end, kind}]. Conservative:
    cat/head/tail/sed -n ranges/grep -n/rg with explicit files; a pipeline is
    split on `|` and each stage parsed. Unknown shapes yield nothing."""
    out: list[dict[str, Any]] = []
    for stage in re.split(r"\|\||&&|;|\|", command):
        try:
            toks = shlex.split(stage.strip())
        except ValueError:
            continue
        if not toks:
            continue
        cmd = toks[0]
        args = toks[1:]
        if cmd in ("cat", "bat", "less", "more"):
            for a in args:
                if not a.startswith("-"):
                    out.append({"path": a, "start": 1, "end": 0, "kind": "cat"})
        elif cmd in ("head", "tail"):
            n = 10
            files: list[str] = []
            i = 0
            while i < len(args):
                a = args[i]
                if a in ("-n", "--lines") and i + 1 < len(args):
                    n = _int(args[i + 1], 10)
                    i += 2
                    continue
                if a.startswith("-n"):
                    n = _int(a[2:], 10)
                elif re.fullmatch(r"-\d+", a):
                    n = _int(a[1:], 10)
                elif not a.startswith("-"):
                    files.append(a)
                i += 1
            for f in files:
                if cmd == "head":
                    out.append({"path": f, "start": 1, "end": n, "kind": "head"})
                else:
                    out.append({"path": f, "start": 0, "end": 0, "kind": "tail", "lines": n})
        elif cmd == "sed":
            rng = None
            files = []
            for a in args:
                m = re.fullmatch(r"(\d+),(\d+)p", a) or re.fullmatch(r"(\d+),(\d+)\s*p", a)
                if m:
                    rng = (int(m.group(1)), int(m.group(2)))
                elif re.fullmatch(r"(\d+)p", a):
                    k = int(a[:-1])
                    rng = (k, k)
                elif not a.startswith("-") and rng is not None:
                    files.append(a)
            if rng:
                for f in files:
                    out.append({"path": f, "start": rng[0], "end": rng[1], "kind": "sed"})
        elif cmd in ("grep", "rg", "egrep", "fgrep"):
            positional = [a for a in args if not a.startswith("-")]
            recursive = cmd == "rg" or any(
                re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*", a) or a == "--recursive" for a in args
            )
            for p in positional[1:]:
                out.append(
                    {
                        "path": p,
                        "start": 0,
                        "end": 0,
                        "kind": "grep_tree" if recursive else "grep_file",
                    }
                )
            if not positional[1:] and recursive:
                out.append({"path": ".", "start": 0, "end": 0, "kind": "grep_tree"})
    return out


def _int(s: str, default: int) -> int:
    try:
        return int(s)
    except ValueError:
        return default


def parse_transcript(path: Path, workdir: str | None = None) -> Transcript:
    """stream-json (`claude -p --output-format stream-json --verbose`) ->
    Transcript. Reads with a span, greps with hits, Bash reads, loom tool
    results with the paths they mention, the final FILES: answer, cost."""
    tool_uses: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    results: dict[str, str] = {}
    final_text = ""
    cost = 0.0
    tin = tout = tcache = 0
    turns = 0
    plugins: list[str] = []
    mcp: list[str] = []
    denied_ids: set[str] = set()
    result_seen = False
    key_source = ""
    for ln in path.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            plugins = [str(p.get("name")) for p in d.get("plugins") or []]
            key_source = str(d.get("apiKeySource") or "")
            mcp = [
                str(s.get("name", s)) if isinstance(s, dict) else str(s)
                for s in d.get("mcp_servers") or []
            ]
            if workdir is None:
                workdir = d.get("cwd")
        elif t == "assistant":
            for c in d.get("message", {}).get("content", []) or []:
                if c.get("type") == "tool_use":
                    tool_uses[c["id"]] = {"name": c.get("name", ""), "input": c.get("input") or {}}
                    order.append(c["id"])
                elif c.get("type") == "text":
                    final_text = c.get("text", "") or final_text
        elif t == "user":
            for c in d.get("message", {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    results[c.get("tool_use_id", "")] = _text_of(c.get("content"))
        elif t == "result":
            result_seen = True
            cost = float(d.get("total_cost_usd") or 0.0)
            u = d.get("usage") or {}
            tin = int(u.get("input_tokens") or 0)
            tout = int(u.get("output_tokens") or 0)
            tcache = int(u.get("cache_read_input_tokens") or 0) + int(
                u.get("cache_creation_input_tokens") or 0
            )
            turns = int(d.get("num_turns") or 0)
            for pd in d.get("permission_denials") or []:
                denied_ids.add(str(pd.get("tool_use_id", "")))
            if d.get("result") and not final_text:
                final_text = str(d.get("result"))

    events: list[ReadEvent] = []
    loom_paths: list[str] = []
    seen_loom: set[str] = set()
    flags: set[str] = set()
    loom_calls = denied = bypass = 0
    for i, tid in enumerate(order):
        tu = tool_uses[tid]
        name, inp = tu["name"], tu["input"]
        res = results.get(tid, "")
        is_denied = tid in denied_ids or res.lstrip().startswith("loom-code-shunt")
        rb = len(res.encode("utf-8", "replace"))
        if LOOM_TOOL_RE.match(name):
            loom_calls += 1
            for p in PATH_RE.findall(res):
                if p not in seen_loom:
                    seen_loom.add(p)
                    loom_paths.append(p)
                    events.append(ReadEvent(i, "loom", p, 0, 0, False, "loom", 0, {"tool": name}))
            continue
        if name in ("WebFetch", "WebSearch"):
            flags.add("web_tool")
        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            flags.add("edit")
            rp = rel_path(str(inp.get("file_path", "")), workdir)
            events.append(ReadEvent(i, "Edit", rp, 0, 0, is_denied, "agent", rb, inp))
            continue
        if name == "Read":
            rp = rel_path(str(inp.get("file_path", "")), workdir)
            if is_denied:
                denied += 1
                events.append(ReadEvent(i, "Read", rp, 0, 0, True, "agent", rb, inp))
                continue
            s, e = _span_from_numbered(res, inp.get("offset"), inp.get("limit"))
            events.append(ReadEvent(i, "Read", rp, s, e, False, "agent", rb, inp))
            continue
        if name == "Grep":
            root = rel_path(str(inp.get("path") or workdir or ""), workdir)
            if is_denied:
                denied += 1
                events.append(ReadEvent(i, "Grep", root, 0, 0, True, "agent", rb, inp))
                continue
            hits = GREP_HIT_RE.findall(res)
            if hits:
                for p, lno in hits:
                    rp = rel_path(p, workdir)
                    if rp is not None:
                        events.append(
                            ReadEvent(i, "Grep", rp, int(lno), int(lno), False, "agent", 0, inp)
                        )
            else:
                for p in PATH_RE.findall(res):
                    events.append(ReadEvent(i, "Grep", p, 0, 0, False, "agent", 0, inp))
            events.append(ReadEvent(i, "Grep", root, 0, 0, False, "agent", rb, inp))
            continue
        if name == "Bash":
            cmd = str(inp.get("command", ""))
            if NETWORK_RE.search(cmd):
                flags.add("network")
            if HISTORY_RE.search(cmd):
                flags.add("git_history")
            if BYPASS_RE.search(cmd):
                bypass += 1
            if is_denied:
                denied += 1
                events.append(ReadEvent(i, "Bash", None, 0, 0, True, "agent", rb, inp))
                continue
            reads = parse_bash_reads(cmd)
            for r in reads:
                rp = rel_path(str(r["path"]), workdir)
                if r["kind"] == "cat":
                    e = max(1, res.count("\n"))
                    events.append(ReadEvent(i, "Bash", rp, 1, e, False, "agent", rb, inp))
                elif r["kind"] == "tail":
                    events.append(ReadEvent(i, "Bash", rp, 0, 0, False, "agent", rb, inp))
                elif r["kind"] in ("grep_file", "grep_tree"):
                    for p, lno in GREP_HIT_RE.findall(res):
                        hp = rel_path(p, workdir)
                        if hp is not None:
                            events.append(
                                ReadEvent(i, "Bash", hp, int(lno), int(lno), False, "agent", 0, inp)
                            )
                    events.append(
                        ReadEvent(i, "Bash", rp if rp else None, 0, 0, False, "agent", rb, inp)
                    )
                else:
                    events.append(
                        ReadEvent(
                            i, "Bash", rp, int(r["start"]), int(r["end"]), False, "agent", rb, inp
                        )
                    )
            if not reads:
                events.append(ReadEvent(i, "Bash", None, 0, 0, False, "agent", rb, inp))
            continue
        events.append(ReadEvent(i, "other", None, 0, 0, is_denied, "agent", rb, {"tool": name}))

    answered = parse_files_line(final_text)
    return Transcript(
        events=events,
        loom_paths=loom_paths,
        final_text=final_text,
        answered_files=answered,
        tool_calls=len(order),
        loom_calls=loom_calls,
        denied_calls=denied,
        bypass_calls=bypass,
        cost_usd=cost,
        tokens_in=tin,
        tokens_out=tout,
        tokens_cache=tcache,
        num_turns=turns,
        plugins=plugins,
        mcp_servers=mcp,
        flags=sorted(flags),
        result_seen=result_seen,
        api_key_source=key_source,
    )


def parse_files_line(text: str) -> list[str]:
    matches = list(re.finditer(r"FILES:\s*(.+)", text or ""))
    if not matches:
        return []
    raw = matches[-1].group(1)
    out: list[str] = []
    for part in re.split(r"[,\s]+", raw):
        p = part.strip().strip("`*'\"").rstrip(".")
        if "/" in p and p not in out:
            out.append(p)
    return out


# ===========================================================================
# scoring
# ===========================================================================


def gold_matches(path: str, gold: Iterable[str]) -> bool:
    """A path matches a gold file when equal or when their mechanism prefixes
    (path minus extension) agree — the .h of a gold .cpp counts."""
    if not path:
        return False
    pm = path.rsplit(".", 1)[0]
    for g in gold:
        if path == g or pm == g.rsplit(".", 1)[0]:
            return True
    return False


def _line_set(spans: Iterable[Sequence[int]], pad: int) -> set[int]:
    out: set[int] = set()
    for s, e in spans:
        if s <= 0:
            continue
        out.update(range(max(1, s - pad), max(s, e) + pad + 1))
    return out


@dataclass
class CellScore:
    task_id: str
    arm: str
    seed: int
    excluded: bool
    exclusion_reasons: list[str]
    fix_in_index: bool
    # localization
    view_b_files: list[str]
    view_a_files: list[str]
    acc5_view_b: float
    acc5_view_a: float
    acc5_answered: float
    first_gold_rank_b: int | None
    # precision / recall over files the agent itself read (View B)
    file_precision: float | None
    file_recall: float
    line_precision_event: float | None
    line_precision_union: float | None
    line_coverage: float | None
    wasted_read_rate: float | None
    redundancy: float | None
    symbol_precision: float | None
    symbol_coverage: float | None
    # cost
    read_events: int
    tool_calls: int
    loom_calls: int
    denied_calls: int
    bypass_calls: int
    result_bytes: int
    tokens_total: int
    cost_usd: float
    num_turns: int
    plugins: list[str]
    mcp_servers: list[str]
    hook_fired: bool  # plugin arm: at least one shunt decision was logged / denied
    api_key_source: str = ""  # who paid for the cell (init row apiKeySource)
    wall_s: float | None = None  # driver wall clock per cell; confounded, never a gate
    cap_hits: int = 0  # arm D: Read calls the read-window cap rewrote


def score_cell(
    task: Task,
    tr: Transcript,
    *,
    arm: str,
    seed: int,
    pad: int = 3,
    shunt_log_lines: int = 0,
    symbol_resolver: Callable[[list[tuple[str, int]]], dict[tuple[str, int], str]] | None = None,
    extra_exclusions: Iterable[str] = (),
    wall_s: float | None = None,
    cap_hits: int = 0,
) -> CellScore:
    gold_files = list(task.gold_files)
    gold_ext = task.gold_ext
    # ordered distinct files, View B (agent provenance) and View A (+ loom)
    view_b: list[str] = []
    view_a: list[str] = []
    for ev in sorted(tr.events, key=lambda e: e.order):
        if ev.denied or not ev.path or ev.path == "":
            continue
        if ev.tool in ("other",):
            continue
        if ev.provenance == "agent" and ev.tool != "Grep" or (ev.tool == "Grep" and ev.start > 0):
            if ev.path not in view_b:
                view_b.append(ev.path)
        if ev.path not in view_a:
            view_a.append(ev.path)

    def acc5(files: Sequence[str]) -> float:
        return 1.0 if any(gold_matches(f, gold_files) for f in files[:5]) else 0.0

    rank_b = next((i + 1 for i, f in enumerate(view_b) if gold_matches(f, gold_files)), None)

    # spans: only agent reads with a line span count as "read lines"
    reads = [
        e
        for e in tr.events
        if e.provenance == "agent"
        and not e.denied
        and e.path
        and e.start > 0
        and e.tool in ("Read", "Bash")
    ]
    gold_lines = {p: _line_set(s, pad) for p, s in task.gold_spans.items()}
    per_event: list[float] = []
    union: dict[str, set[int]] = {}
    total_lines = 0
    wasted = 0
    for e in reads:
        n = e.end - e.start + 1
        total_lines += n
        lines = set(range(e.start, e.end + 1))
        g = gold_lines.get(e.path or "", set())
        hit = len(lines & g)
        per_event.append(hit / n if n else 0.0)
        if not gold_matches(e.path or "", gold_ext):
            wasted += 1
        union.setdefault(e.path or "", set()).update(lines)
    union_lines = sum(len(v) for v in union.values())
    union_hit = sum(len(v & gold_lines.get(p, set())) for p, v in union.items())
    gold_total = sum(len(v) for v in gold_lines.values())
    covered = sum(len(gold_lines.get(p, set()) & v) for p, v in union.items())

    read_files = [f for f in view_b]
    fp = None
    if read_files:
        fp = sum(1 for f in read_files if gold_matches(f, gold_ext)) / len(read_files)
    fr = 0.0
    if gold_files:
        found = sum(1 for g in gold_files if any(gold_matches(f, [g]) for f in read_files))
        fr = found / len(gold_files)

    sym_p = sym_c = None
    if symbol_resolver is not None and reads:
        want: list[tuple[str, int]] = []
        for e in reads:
            if e.path:
                want.append((e.path, e.start))
                want.append((e.path, e.end))
        for p, spans in task.gold_spans.items():
            for s, _e in spans:
                want.append((p, s))
        resolved = symbol_resolver(sorted(set(want)))
        read_syms = {
            resolved[(e.path or "", ln)]
            for e in reads
            for ln in (e.start, e.end)
            if (e.path or "", ln) in resolved and resolved[(e.path or "", ln)]
        }
        gold_syms = {
            resolved[(p, s)]
            for p, spans in task.gold_spans.items()
            for s, _e in spans
            if (p, s) in resolved and resolved[(p, s)]
        }
        if read_syms:
            sym_p = len(read_syms & gold_syms) / len(read_syms)
        if gold_syms:
            sym_c = len(read_syms & gold_syms) / len(gold_syms)

    reasons = sorted(set(tr.flags) & {"network", "git_history", "web_tool"})
    reasons += list(extra_exclusions)
    if task.leak_dropped:
        reasons.append("task_text_leak")
    if not tr.result_seen:
        reasons.append("no_result_row")
    plugin_arm = arm != "A"
    hook_fired = (shunt_log_lines > 0 or tr.denied_calls > 0) if plugin_arm else False
    return CellScore(
        task_id=task.id,
        arm=arm,
        seed=seed,
        excluded=bool(reasons),
        exclusion_reasons=reasons,
        fix_in_index=task.fix_in_index,
        view_b_files=view_b,
        view_a_files=view_a,
        acc5_view_b=acc5(view_b),
        acc5_view_a=acc5(view_a),
        acc5_answered=acc5(tr.answered_files),
        first_gold_rank_b=rank_b,
        file_precision=fp,
        file_recall=fr,
        line_precision_event=(sum(per_event) / len(per_event)) if per_event else None,
        line_precision_union=(union_hit / union_lines) if union_lines else None,
        line_coverage=(covered / gold_total) if gold_total else None,
        wasted_read_rate=(wasted / len(reads)) if reads else None,
        redundancy=((total_lines - union_lines) / total_lines) if total_lines else None,
        symbol_precision=sym_p,
        symbol_coverage=sym_c,
        read_events=len(reads),
        tool_calls=tr.tool_calls,
        loom_calls=tr.loom_calls,
        denied_calls=tr.denied_calls,
        bypass_calls=tr.bypass_calls,
        result_bytes=sum(e.result_bytes for e in tr.events),
        tokens_total=tr.tokens_in + tr.tokens_out + tr.tokens_cache,
        cost_usd=tr.cost_usd,
        num_turns=tr.num_turns,
        plugins=tr.plugins,
        mcp_servers=tr.mcp_servers,
        hook_fired=hook_fired,
        api_key_source=tr.api_key_source,
        wall_s=wall_s,
        cap_hits=cap_hits,
    )


# ===========================================================================
# statistics: paired Wilcoxon signed-rank, paired bootstrap lower bound
# ===========================================================================


def wilcoxon_signed_rank(x: Sequence[float], y: Sequence[float]) -> tuple[float, int]:
    """Two-sided paired Wilcoxon signed-rank p-value and the number of
    non-zero pairs. Exact for n <= 20 (enumeration of sign patterns), the
    normal approximation with tie correction above. No scipy."""
    d = [a - b for a, b in zip(x, y, strict=True) if a != b]
    n = len(d)
    if n == 0:
        return 1.0, 0
    absd = sorted((abs(v), i) for i, v in enumerate(d))
    ranks = [0.0] * n
    i = 0
    tie_groups: list[int] = []
    while i < n:
        j = i
        while j + 1 < n and absd[j + 1][0] == absd[i][0]:
            j += 1
        r = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[absd[k][1]] = r
        tie_groups.append(j - i + 1)
        i = j + 1
    w_plus = sum(r for r, v in zip(ranks, d, strict=True) if v > 0)
    w_minus = sum(r for r, v in zip(ranks, d, strict=True) if v < 0)
    w = min(w_plus, w_minus)
    if n <= 20:
        total = 0
        count = 0
        for signs in itertools.product((0, 1), repeat=n):
            s = sum(r for r, sg in zip(ranks, signs, strict=True) if sg)
            total += 1
            if s <= w + 1e-9:
                count += 1
        p = min(1.0, 2.0 * count / total)
        return p, n
    mean = n * (n + 1) / 4.0
    var = n * (n + 1) * (2 * n + 1) / 24.0 - sum(t**3 - t for t in tie_groups) / 48.0
    if var <= 0:
        return 1.0, n
    z = (w - mean) / math.sqrt(var)
    p = 2.0 * (1.0 - _phi(abs(z)))
    return min(1.0, p), n


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def paired_bootstrap_lb(
    x: Sequence[float],
    y: Sequence[float],
    *,
    iters: int = 2000,
    alpha: float = 0.05,
    seed: int = 20260914,
) -> tuple[float, float, float]:
    """(mean delta, lower bound, upper bound) of mean(x - y) by paired
    bootstrap over tasks."""
    d = [a - b for a, b in zip(x, y, strict=True)]
    if not d:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(d)
    means = sorted(statistics.fmean(rng.choice(d) for _ in range(n)) for _ in range(iters))
    lo = means[int(alpha / 2 * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return statistics.fmean(d), lo, hi


# ===========================================================================
# aggregation + report
# ===========================================================================

METRICS: tuple[str, ...] = (
    "acc5_view_b",
    "acc5_view_a",
    "acc5_answered",
    "file_precision",
    "file_recall",
    "line_precision_event",
    "line_precision_union",
    "line_coverage",
    "wasted_read_rate",
    "redundancy",
    "symbol_precision",
    "symbol_coverage",
    "read_events",
    "tool_calls",
    "loom_calls",
    "denied_calls",
    "bypass_calls",
    "result_bytes",
    "tokens_total",
    "cost_usd",
    "num_turns",
    "wall_s",
    "cap_hits",
)


def per_task_means(cells: Iterable[CellScore]) -> dict[str, dict[str, dict[str, float | None]]]:
    """arm -> task -> metric -> mean over seeds (None when every seed lacks it)."""
    by: dict[str, dict[str, list[CellScore]]] = {}
    for c in cells:
        if c.excluded:
            continue
        by.setdefault(c.arm, {}).setdefault(c.task_id, []).append(c)
    out: dict[str, dict[str, dict[str, float | None]]] = {}
    for arm, tasks in by.items():
        out[arm] = {}
        for tid, cs in tasks.items():
            row: dict[str, float | None] = {}
            for m in METRICS:
                vals = [float(getattr(c, m)) for c in cs if getattr(c, m) is not None]
                row[m] = statistics.fmean(vals) if vals else None
            out[arm][tid] = row
    return out


def compare_arms(
    means: dict[str, dict[str, dict[str, float | None]]], a: str, b: str, metric: str
) -> dict[str, Any] | None:
    ta, tb = means.get(a, {}), means.get(b, {})
    common = [
        t for t in ta if t in tb and ta[t].get(metric) is not None and tb[t].get(metric) is not None
    ]
    if not common:
        return None
    x = [float(ta[t][metric] or 0.0) for t in common]
    y = [float(tb[t][metric] or 0.0) for t in common]
    p, nz = wilcoxon_signed_rank(x, y)
    delta, lo, hi = paired_bootstrap_lb(x, y)
    return {
        "arms": f"{a}-{b}",
        "metric": metric,
        "n": len(common),
        "nonzero_pairs": nz,
        "delta": delta,
        "lb": lo,
        "ub": hi,
        "wilcoxon_p": p,
    }


def arm_summary(cells: Sequence[CellScore]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for arm in sorted({c.arm for c in cells}):
        cs = [c for c in cells if c.arm == arm]
        kept = [c for c in cs if not c.excluded]
        row: dict[str, Any] = {
            "cells": len(cs),
            "excluded": len(cs) - len(kept),
            "hook_fired_share": (
                (sum(c.hook_fired for c in kept) / len(kept)) if kept and arm != "A" else None
            ),
            "plugin_loaded_share": (
                (sum(bool(c.plugins) for c in kept) / len(kept)) if kept and arm != "A" else None
            ),
            "fix_in_index_share": (sum(c.fix_in_index for c in kept) / len(kept)) if kept else None,
            # share of kept cells billed to the bench key rather than a login
            "api_key_share": (
                (sum(c.api_key_source == "ANTHROPIC_API_KEY" for c in kept) / len(kept))
                if kept
                else None
            ),
            "api_key_sources": sorted({c.api_key_source or "?" for c in kept}),
        }
        for m in METRICS:
            vals = [float(getattr(c, m)) for c in kept if getattr(c, m) is not None]
            row[m] = statistics.fmean(vals) if vals else None
        solved = sum(c.acc5_view_b for c in kept)
        row["usd_per_solved"] = (sum(c.cost_usd for c in kept) / solved) if solved else None
        out[arm] = row
    return out


def write_exclusions(cells: Sequence[CellScore], path: Path) -> int:
    rows = [c for c in cells if c.excluded]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["task_id", "arm", "seed", "reasons"])
        for c in rows:
            w.writerow([c.task_id, c.arm, c.seed, ";".join(c.exclusion_reasons)])
    return len(rows)


def write_unified(cells: Sequence[tuple[Task, Transcript, CellScore]], path: Path) -> None:
    """One row per cell in the cross-scorer shape: the agent's read spans
    with provenance, the gold, the answer."""
    with path.open("w") as fh:
        for task, tr, sc in cells:
            row = {
                "instance_id": task.id,
                "arm": sc.arm,
                "seed": sc.seed,
                "index_head": task.index_head,
                "base_sha": task.base_sha,
                "fix_in_index": task.fix_in_index,
                "reads": [
                    {
                        "tool": e.tool,
                        "path": e.path,
                        "start": e.start,
                        "end": e.end,
                        "denied": e.denied,
                        "provenance": e.provenance,
                    }
                    for e in tr.events
                    if e.path
                ],
                "gold": {
                    "files": task.gold_files,
                    "callers": task.gold_callers,
                    "spans": task.gold_spans,
                },
                "answered": tr.answered_files,
                "excluded": sc.excluded,
            }
            fh.write(json.dumps(row) + "\n")


def render_report(
    summary: dict[str, dict[str, Any]],
    comparisons: list[dict[str, Any]],
    n_excluded: int,
    tasks: Sequence[Task],
    stats: dict[str, int],
    scored_task_ids: Iterable[str] = (),
) -> str:
    lines = ["# code plane agent ablation", ""]
    scored = set(scored_task_ids)
    run = [t for t in tasks if not t.leak_dropped and (not scored or t.id in scored)]
    dropped = [t for t in tasks if t.leak_dropped]
    lines.append(
        f"tasks run: {len(run)} of {sum(1 for t in tasks if not t.leak_dropped)} runnable;"
        f" dropped for a gold stem in the task text: {len(dropped)}"
        f" ({(len(dropped) / len(tasks) * 100) if tasks else 0:.0f}% of mined)"
    )
    if stats:
        lines.append("miner: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    fii = sum(t.fix_in_index for t in run)
    lines.append(f"fix_in_index: {fii}/{len(run)} tasks (upper-bound caveat when > 0)")
    lines.append(f"excluded cells (exclusions.csv): {n_excluded}")
    lines.append("")
    cols = [
        "arm",
        "cells",
        "excl",
        "plugin",
        "hook",
        "acc5_B",
        "acc5_A",
        "acc5_ans",
        "file_P",
        "file_R",
        "line_P",
        "waste",
        "reads",
        "calls",
        "loom",
        "denied",
        "bypass",
        "cap",
        "kB",
        "tokens",
        "$/task",
        "$/solved",
        "wall_s",
        "billed",
    ]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "---|" * len(cols))
    for arm, r in summary.items():

        def f(v: Any, nd: int = 2) -> str:
            return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))

        lines.append(
            "| "
            + " | ".join(
                [
                    arm,
                    str(r["cells"]),
                    str(r["excluded"]),
                    f(r["plugin_loaded_share"]),
                    f(r["hook_fired_share"]),
                    f(r["acc5_view_b"]),
                    f(r["acc5_view_a"]),
                    f(r["acc5_answered"]),
                    f(r["file_precision"]),
                    f(r["file_recall"]),
                    f(r["line_precision_event"]),
                    f(r["wasted_read_rate"]),
                    f(r["read_events"], 1),
                    f(r["tool_calls"], 1),
                    f(r["loom_calls"], 1),
                    f(r["denied_calls"], 1),
                    f(r["bypass_calls"], 1),
                    f(r.get("cap_hits"), 1),
                    f((r["result_bytes"] or 0) / 1024.0, 0)
                    if r["result_bytes"] is not None
                    else "-",
                    f(r["tokens_total"], 0),
                    f(r["cost_usd"], 3),
                    f(r["usd_per_solved"], 2),
                    f(r.get("wall_s"), 0),
                    ",".join(r.get("api_key_sources") or []) or "-",
                ]
            )
            + " |"
        )
    total_cost = sum(
        float(r.get("cost_usd") or 0.0) * (r["cells"] - r["excluded"]) for r in summary.values()
    )
    lines.append("")
    lines.append(
        f"CLI-accounted spend over kept cells: ${total_cost:.2f} "
        "(sum of result-row total_cost_usd; the ledger's server-side rows are separate)"
    )
    if comparisons:
        lines += [
            "",
            "| pair | metric | n | delta | 95% LB | 95% UB | Wilcoxon p |",
            "|---|---|---|---|---|---|---|",
        ]
        for c in comparisons:
            lines.append(
                f"| {c['arms']} | {c['metric']} | {c['n']} | {c['delta']:+.3f} | "
                f"{c['lb']:+.3f} | {c['ub']:+.3f} | {c['wilcoxon_p']:.3f} |"
            )
    return "\n".join(lines) + "\n"


# ===========================================================================
# live helpers (read-only): callers via CH, symbols via code.enclosing
# ===========================================================================


def ch_callers_fn(
    ch_query: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
    org: str,
    namespace: str,
    min_conf: float = 0.8,
) -> Callable[[list[str]], list[str]]:
    """1-hop callers of every live symbol defined in the gold files, at edge
    confidence >= min_conf, as src/ paths. `ch_query(sql, params)` runs a
    read-only query (the caller supplies the client)."""

    def callers(files: list[str]) -> list[str]:
        rows = ch_query(
            """
            SELECT DISTINCT e.path AS path
            FROM loom.code_edges AS e
            WHERE e.org = {org:String} AND e.namespace = {ns:String}
              AND e.edge_kind = 'calls' AND e.tombstoned_at IS NULL
              AND e.confidence >= {conf:Float32}
              AND e.to_symbol_id IN (
                  SELECT symbol_id FROM loom.code_symbols
                  WHERE org = {org:String} AND namespace = {ns:String}
                    AND tombstoned_at IS NULL AND path IN {files:Array(String)}
              )
              AND startsWith(e.path, 'src/')
            LIMIT 200
            """,
            {"org": org, "ns": namespace, "conf": min_conf, "files": files},
        )
        return sorted({str(r["path"]) for r in rows})

    return callers


def http_symbol_resolver(
    base_url: str, token: str, org: str, namespace: str
) -> Callable[[list[tuple[str, int]]], dict[tuple[str, int], str]]:
    """Batched code.enclosing (<= 200 locations per call) -> symbol_id per
    (path, line); unresolved locations map to ''."""
    import httpx

    def resolve(locs: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
        out: dict[tuple[str, int], str] = {}
        with httpx.Client(
            base_url=base_url, timeout=60.0, headers={"Authorization": f"Bearer {token}"}
        ) as client:
            for i in range(0, len(locs), 200):
                batch = locs[i : i + 200]
                r = client.post(
                    "/v1/code.enclosing",
                    json={
                        "org": org,
                        "namespace": namespace,
                        "consumer": "bench-code-plane-ablation",
                        "locations": [f"{p}:{ln}" for p, ln in batch],
                    },
                )
                if r.status_code != 200:
                    continue
                body = r.json()
                ents = body.get("locations") or body.get("results") or []
                for loc, ent in zip(batch, ents, strict=False):
                    ent = ent or {}
                    out[loc] = str(ent.get("symbol_id") or ent.get("qualified_name") or "")
        return out

    return resolve


# ===========================================================================
# CLI
# ===========================================================================


def _cells_dir(run_dir: Path) -> Path:
    return run_dir / "cells"


def gh_merged_prs(owner_repo: str, limit: int) -> list[tuple[str, int]]:
    """(merge_commit_sha, number) of the most recently merged PRs, newest first, through
    `gh api` — for a repository whose commit subjects name no PR (a repository that squash-merges
    without `(#N)`)."""
    out: list[tuple[str, int]] = []
    page = 1
    while len(out) < limit and page <= 10:
        res = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner_repo}/pulls?state=closed&sort=updated&direction=desc"
                f"&per_page=100&page={page}",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if res.returncode != 0:
            break
        rows = json.loads(res.stdout or "[]")
        if not rows:
            break
        for r in rows:
            sha = str(r.get("merge_commit_sha") or "")
            if r.get("merged_at") and sha:
                out.append((sha, int(r["number"])))
        page += 1
    return out[:limit]


def cmd_mine(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    fetch = gh_pr_fetcher(args.gh_repo) if args.gh_repo else None
    callers = None
    if args.ch_callers:
        callers = ch_callers_fn(_ch_query_factory(), args.org, args.namespace)
    merged = gh_merged_prs(args.gh_repo, args.scan) if args.gh_merged else None
    tasks, stats = mine_tasks(
        repo,
        args.index_head,
        args.namespace,
        args.n,
        ref=args.ref,
        scan=args.scan,
        fetch_body=fetch,
        callers=callers,
        min_body_chars=args.min_body_chars,
        squash=bool(args.squash),
        merged_prs=merged,
        gold_prefix=args.gold_prefix,
        min_files=args.min_files,
        max_files=args.max_files,
    )
    write_tasks(tasks, stats, Path(args.out))
    print(json.dumps(stats))
    print(f"wrote {len(tasks)} task rows ({stats['kept']} runnable) -> {args.out}")
    return 0


def _ch_query_factory() -> Callable[[str, dict[str, Any]], list[dict[str, Any]]]:
    """Read-only ClickHouse via the server's own client, with the .env that
    LOOM_ENV_FILE names loaded line by line (JSON values intact) — the
    isolated-server pattern."""
    import asyncio

    env_file = os.environ.get("LOOM_ENV_FILE", "")  # no host default
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            line = line.rstrip("\n")
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if v[:1] not in "{\"'" and " #" in v:
                v = v.split(" #", 1)[0].rstrip()
            if v[:1] in "\"'" and v[-1:] == v[:1]:
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)
    os.environ["LOOM_BACKGROUND_WORKERS"] = "off"
    sys.path.insert(0, str(HERE.parent))
    from server.core.ch import get_client

    def run(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        async def go() -> list[dict[str, Any]]:
            ch = get_client()
            await ch.connect()
            try:
                return list(await ch.query_all(sql, params))
            finally:
                try:
                    await ch.close()
                except Exception:  # noqa: BLE001 - best effort on a one-shot client
                    pass

        return asyncio.run(go())

    return run


def parse_arm_urls(specs: Iterable[str]) -> dict[str, str]:
    """`--arm-url E=http://127.0.0.1:7862` -> {"E": url}; malformed specs raise."""
    out: dict[str, str] = {}
    for spec in specs:
        arm, sep, url = spec.partition("=")
        if not sep or not arm.strip() or not url.strip():
            raise ValueError(f"--arm-url wants ARM=URL, got {spec!r}")
        out[arm.strip()] = url.strip()
    return out


def parse_arm_env(specs: Iterable[str]) -> dict[str, dict[str, str]]:
    """`--arm-env F=LOOM_SHUNT_READ_CAP=200` -> {"F": {"LOOM_SHUNT_READ_CAP": "200"}};
    repeatable per arm; malformed specs raise."""
    out: dict[str, dict[str, str]] = {}
    for spec in specs:
        arm, sep, kv = spec.partition("=")
        name, sep2, value = kv.partition("=")
        if not sep or not sep2 or not arm.strip() or not name.strip():
            raise ValueError(f"--arm-env wants ARM=NAME=VALUE, got {spec!r}")
        out.setdefault(arm.strip(), {})[name.strip()] = value
    return out


def _token(token_file: str) -> str:
    if token_file:
        return Path(token_file).read_text().strip()
    return os.environ.get("LOOM_API_KEY", "")


def cmd_run(args: argparse.Namespace) -> int:
    from bench.agent_traj.claude_code_driver import (
        Cell,
        ClaudeCliDriver,
        DriverConfig,
        DryRunDriver,
        FixtureDriver,
        repo_slug_of_mirror,
    )

    tasks, _ = load_tasks(Path(args.tasks))
    tasks = [t for t in tasks if not t.leak_dropped]
    if args.limit:
        tasks = tasks[: args.limit]
    # absolute: `git -C <mirror> worktree add <path>` resolves a relative path
    # against the MIRROR, not the caller's cwd, and would plant the worktree
    # inside the bare repository
    run_dir = Path(args.out).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    token = _token(args.token_file)
    arm_urls = parse_arm_urls(args.arm_url or [])
    arm_plugins = {a: Path(p) for a, p in parse_arm_urls(args.arm_plugin_src or []).items()}
    arm_env = parse_arm_env(args.arm_env or [])
    if (
        args.driver == "claude"
        and args.billing == "api-key"
        and not os.environ.get("ANTHROPIC_API_KEY")
    ):
        print(
            "refusing to run: --billing api-key needs ANTHROPIC_API_KEY in the environment "
            "(the CLI would otherwise bill the logged-in account; pass --billing login "
            "to accept that)",
            file=sys.stderr,
        )
        return 2
    shunt_repo = args.shunt_repo or (repo_slug_of_mirror(args.repo) if args.repo else "")
    if any(a != "A" for a in arms) and not shunt_repo:
        print(
            "refusing to run: no --shunt-repo and the mirror has no origin URL to derive "
            "owner/name from — the plugin arms' hook would resolve the checkout as "
            "not indexed",
            file=sys.stderr,
        )
        return 2
    cfg = DriverConfig(
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
        timeout_s=args.timeout,
        plugin_src=Path(args.plugin_src) if args.plugin_src else None,
        loom_url=args.loom_url,
        loom_token=token,
        shunt_repo=shunt_repo,
        read_cap=args.read_cap,
        shunt_report=args.shunt_report,
        arm_urls=arm_urls,
        arm_plugin_src=arm_plugins,
        arm_env=arm_env,
    )
    cli = ClaudeCliDriver(cfg, run_dir, claude_bin=args.claude_bin)
    driver: Any
    if args.driver == "dry":
        driver = DryRunDriver(cli)
    elif args.driver == "fixture":
        driver = FixtureDriver(Path(args.fixture))
    else:
        driver = cli
    repo = Path(args.repo) if args.repo else None
    manifest = run_dir / "manifest.jsonl"
    done: set[str] = set()
    if manifest.exists():
        for ln in manifest.read_text().splitlines():
            if ln.strip():
                d = json.loads(ln)
                done.add(f"{d['task_id']}:{d['arm']}:{d['seed']}")
    total = failed = 0
    for t in tasks:
        workdir = run_dir / "worktrees" / t.id
        if repo is not None and args.driver == "claude":
            if not workdir.exists():
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "worktree",
                        "add",
                        "--detach",
                        str(workdir),
                        t.base_sha,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            head = subprocess.run(
                ["git", "-C", str(workdir), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout.strip()
            if head != t.base_sha:  # a reused worktree must still sit at the task's base
                print(f"refusing to run: {workdir} is at {head}, not {t.base_sha}", file=sys.stderr)
                return 2
        pending: list[Cell] = []
        for seed in range(1, args.seeds + 1):
            for arm in arms:
                if f"{t.id}:{arm}:{seed}" in done:
                    continue
                pending.append(
                    Cell(
                        task_id=t.id,
                        arm=arm,
                        seed=seed,
                        prompt=t.prompt,
                        system_prompt=build_system_prompt(arm, t.namespace, t.index_head, args.org),
                        base_sha=t.base_sha,
                        cell_dir=_cells_dir(run_dir) / t.id / arm / f"s{seed}",
                        workdir=workdir,
                    )
                )
        # a task's cells run concurrently (the worktree is read-only); the
        # arms of one task then see the same server load, which is the
        # pairing the statistics assume
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for meta in pool.map(driver.run, pending):
                key = f"{meta['task_id']}:{meta['arm']}:{meta['seed']}"
                meta["trace_run_id"] = f"bench:code_plane_ablation:{key}"
                with manifest.open("a") as fh:
                    fh.write(json.dumps(meta) + "\n")
                total += 1
                # the dry-run driver runs nothing and reports returncode None
                failed += int(
                    bool(meta.get("timed_out")) or meta.get("returncode") not in (0, None)
                )
                print(
                    f"[{total}] {key} rc={meta.get('returncode')} {meta.get('duration_s')}s",
                    file=sys.stderr,
                )
        if repo is not None and args.driver == "claude" and args.remove_worktrees:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(workdir)],
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
    print(f"ran {total} cells ({failed} timed out or exited non-zero) -> {run_dir}")
    return 1 if failed else 0


SymbolResolver = Callable[[list[tuple[str, int]]], dict[tuple[str, int], str]]


def score_run(
    run_dir: Path,
    tasks: Sequence[Task],
    *,
    pad: int = 3,
    symbol_resolver: SymbolResolver | None = None,
) -> tuple[list[CellScore], list[tuple[Task, Transcript, CellScore]]]:
    by_id = {t.id: t for t in tasks}
    scores: list[CellScore] = []
    triples: list[tuple[Task, Transcript, CellScore]] = []
    # the run phase used the absolute path; transcript paths strip against it
    run_dir = run_dir.resolve()
    cells = _cells_dir(run_dir)
    if not cells.exists():
        return scores, triples
    for tdir in sorted(cells.iterdir()):
        task = by_id.get(tdir.name)
        if task is None:
            continue
        for adir in sorted(tdir.iterdir()):
            for sdir in sorted(adir.iterdir()):
                tp = sdir / "transcript.jsonl"
                if not tp.exists():
                    continue
                seed = int(sdir.name.lstrip("s") or "1")
                workdir = str(run_dir / "worktrees" / task.id)
                tr = parse_transcript(tp, workdir=workdir)
                shunt_lines = 0
                sl = sdir / "shunt.log"
                if sl.exists():
                    shunt_lines = sum(1 for ln in sl.read_text().splitlines() if ln.strip())
                cap_hits = 0
                cl = sdir / "read_cap.log"
                if cl.exists():
                    cap_hits = sum(1 for ln in cl.read_text().splitlines() if ln.startswith("cap "))
                wall: float | None = None
                extra: list[str] = []
                mp = sdir / "meta.json"
                if mp.exists():
                    try:
                        meta = json.loads(mp.read_text())
                        wall = float(meta.get("duration_s") or 0.0) or None
                        # A plugin arm whose hook resolved the
                        # checkout as not indexed never used the index — it is
                        # excluded like a leaked cell, not scored as the arm
                        if adir.name != "A" and meta.get("shunt_resolved") is False:
                            extra.append("shunt_unmapped")
                    except (json.JSONDecodeError, TypeError, ValueError):
                        wall = None
                sc = score_cell(
                    task,
                    tr,
                    arm=adir.name,
                    seed=seed,
                    pad=pad,
                    shunt_log_lines=shunt_lines,
                    symbol_resolver=symbol_resolver,
                    extra_exclusions=extra,
                    wall_s=wall,
                    cap_hits=cap_hits,
                )
                scores.append(sc)
                triples.append((task, tr, sc))
    return scores, triples


def cmd_score(args: argparse.Namespace) -> int:
    tasks, stats = load_tasks(Path(args.tasks))
    run_dir = Path(args.runs)
    resolver = None
    if args.resolve_symbols:
        token = _token(args.token_file)
        ns = tasks[0].namespace if tasks else args.namespace
        resolver = http_symbol_resolver(args.loom_url, token, args.org, ns)
    scores, triples = score_run(run_dir, tasks, pad=args.pad, symbol_resolver=resolver)
    n_excl = write_exclusions(scores, run_dir / "exclusions.csv")
    write_unified(triples, run_dir / "unified.jsonl")
    summary = arm_summary(scores)
    means = per_task_means(scores)
    arms = sorted(summary)
    comparisons: list[dict[str, Any]] = []
    for a, b in itertools.combinations(arms, 2):
        for m in (
            "acc5_view_b",
            "file_precision",
            "file_recall",
            "tool_calls",
            "cost_usd",
            "tokens_total",
            "wall_s",
        ):
            c = compare_arms(means, b, a, m)
            if c:
                comparisons.append(c)
    (run_dir / "scores.jsonl").write_text("".join(json.dumps(asdict(s)) + "\n" for s in scores))
    report = {
        "summary": summary,
        "comparisons": comparisons,
        "excluded_cells": n_excl,
        "tasks_run": sum(1 for t in tasks if not t.leak_dropped),
        "tasks_leak_dropped": sum(1 for t in tasks if t.leak_dropped),
        "fix_in_index_tasks": sum(1 for t in tasks if t.fix_in_index and not t.leak_dropped),
        "miner_stats": stats,
    }
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    text = render_report(summary, comparisons, n_excl, tasks, stats, {s.task_id for s in scores})
    (run_dir / "report.md").write_text(text)
    print(text)
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Score the synthetic fixture and assert the numbers it was built to
    produce; CI exercises the scorer without an agent."""
    tasks, _ = load_tasks(FIXTURE_DIR / "tasks.jsonl")
    task = tasks[0]
    tr_a = parse_transcript(FIXTURE_DIR / "transcript_arm_A.jsonl", workdir="/work/ch")
    tr_c = parse_transcript(FIXTURE_DIR / "transcript_arm_C.jsonl", workdir="/work/ch")
    sa = score_cell(task, tr_a, arm="A", seed=1)
    sc = score_cell(task, tr_c, arm="C", seed=1, shunt_log_lines=2)
    assert sa.acc5_view_b == 1.0 and sa.first_gold_rank_b == 3, sa
    assert sc.acc5_view_b == 1.0 and sc.acc5_view_a == 1.0 and sc.first_gold_rank_b == 1, sc
    assert sc.loom_calls == 1 and sc.denied_calls == 1 and sc.hook_fired, sc
    assert sa.tool_calls == 4 and sc.tool_calls == 4
    assert (sa.file_precision or 0) < (sc.file_precision or 0), (
        sa.file_precision,
        sc.file_precision,
    )
    assert not sa.excluded and not sc.excluded
    print(json.dumps({"A": asdict(sa), "C": asdict(sc)}, indent=1)[:1200])
    print("selftest ok")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mine", help="mine PR tasks from a bare mirror")
    m.add_argument("--repo", required=True)
    m.add_argument("--index-head", required=True)
    m.add_argument("--namespace", required=True)
    m.add_argument("--org", default="dev")
    m.add_argument("--ref", default="HEAD")
    m.add_argument("--n", type=int, default=10)
    m.add_argument("--scan", type=int, default=400)
    m.add_argument("--gh-repo", default="", help="owner/name: fetch PR bodies via `gh api`")
    m.add_argument(
        "--min-files",
        type=int,
        default=1,
        help="changed src files a PR needs to be a task ("
        "the multi-file workload the structural index pays on)",
    )
    m.add_argument("--max-files", type=int, default=8)
    m.add_argument("--min-body-chars", type=int, default=0)
    m.add_argument(
        "--ch-callers", action="store_true", help="1-hop callers from ClickHouse (read-only)"
    )
    m.add_argument(
        "--gold-prefix",
        default=GOLD_PREFIX,
        help="the source tree whose changed files are gold (default src/). The prompt, "
        "PATH_RE and the callers query assume the ClickHouse tree, so another repository "
        "needs those changed too",
    )
    m.add_argument(
        "--gh-merged",
        action="store_true",
        help="take the PR list from GitHub (merge_commit_sha) — "
        "needs --gh-repo; for repositories whose commits name no PR",
    )
    m.add_argument(
        "--squash",
        action="store_true",
        help="mine squash-merged PRs (first-parent commits with (#N))",
    )
    m.add_argument("--out", required=True)
    m.set_defaults(fn=cmd_mine)

    r = sub.add_parser("run", help="run cells (claude | fixture | dry)")
    r.add_argument("--tasks", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--arms", default="A,B,C", help="subset of A,B,C,D,E,F (see ARM_DESCRIPTIONS)")
    r.add_argument("--seeds", type=int, default=1)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--driver", choices=("claude", "fixture", "dry"), default="dry")
    r.add_argument("--fixture", default=str(FIXTURE_DIR / "transcript_arm_A.jsonl"))
    r.add_argument(
        "--repo", default="", help="bare mirror for `git worktree add` at the base commit"
    )
    r.add_argument("--remove-worktrees", action="store_true")
    r.add_argument("--plugin-src", default=str(DEFAULT_PLUGIN_SRC))
    r.add_argument("--loom-url", default="http://127.0.0.1:7859")
    r.add_argument(
        "--arm-url",
        action="append",
        default=[],
        metavar="ARM=URL",
        help="per-arm loom URL (arm E: the bodies=none server)",
    )
    r.add_argument(
        "--arm-plugin-src",
        action="append",
        default=[],
        metavar="ARM=PATH",
        help="build this arm's shunt-only plugin from another "
        "plugin tree (arm F: the redirect naming code.grep's one-call block)",
    )
    r.add_argument(
        "--arm-env",
        action="append",
        default=[],
        metavar="ARM=NAME=VALUE",
        help="an env var for one arm's cells only (arm F with "
        "LOOM_SHUNT_READ_CAP=200: arm C plus the plugin's read cap)",
    )
    r.add_argument(
        "--billing",
        choices=("api-key", "login"),
        default="api-key",
        help="api-key refuses to run without ANTHROPIC_API_KEY in the environment",
    )
    r.add_argument("--token-file", default="")
    r.add_argument("--org", default="dev")
    r.add_argument(
        "--shunt-repo",
        default="",
        help="owner/name the shunt resolves the checkout to; default: the "
        "mirror's origin (a wrong slug runs a whole plugin arm with the hook "
        "cached as 'not indexed')",
    )
    r.add_argument(
        "--shunt-report", action="store_true", help="let the hook post shunt rows to the server"
    )
    r.add_argument("--model", default="sonnet")
    r.add_argument("--max-turns", type=int, default=30)
    r.add_argument("--max-budget-usd", type=float, default=1.5)
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--read-cap", type=int, default=200, help="arm D Read limit ceiling (lines)")
    r.add_argument("--claude-bin", default="claude")
    r.add_argument("--jobs", type=int, default=3, help="cells of one task run concurrently")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("score", help="score a run directory")
    s.add_argument("--tasks", required=True)
    s.add_argument("--runs", required=True)
    s.add_argument(
        "--pad", type=int, default=3, help="lines of context around a gold hunk that count"
    )
    s.add_argument("--resolve-symbols", action="store_true")
    s.add_argument("--loom-url", default="http://127.0.0.1:7859")
    s.add_argument("--token-file", default="")
    s.add_argument("--org", default="dev")
    s.add_argument("--namespace", default="code-clickhouse")
    s.set_defaults(fn=cmd_score)

    t = sub.add_parser("selftest", help="score the synthetic fixture")
    t.set_defaults(fn=cmd_selftest)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
