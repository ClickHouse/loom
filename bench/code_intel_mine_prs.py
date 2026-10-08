"""Mine an issue-shaped localization fixture for the code plane from merged PRs.

The code QA fixture (bench/fixtures/code_intel_qa.jsonl) has 36 conceptual
questions; a paired delta on it moves 2.8 points per question, so nothing
under three points is a result (bench/README). Deciding a retrieval leg
takes hundreds of questions per lane. This miner produces the issue-to-files lane from
`loom.code_pr_history` joined to `loom.code_gh_items`, read-only:

  query      the linked issue's title when the PR body links one
             ("fixes #N", "closes #N", "resolves #N"), else the PR title
  gold.paths the PR's changed `src/` files with their extension stripped
             (a mechanism prefix: `src/Parsers/ParserAlias` matches the .h
             and the .cpp, the scorer's own rule)
  lane       "issue" when the query is an issue title, "pr_title" otherwise

Filters, each reported: 1–8 changed `src/` files (a fix, not a sweep),
body over 200 characters, title not a revert, merged inside the window.
Leak filter: a question whose text contains a gold file's stem is dropped
and counted — an agent could grep the answer out of the question.

Caveat the fixture carries in its header: the live index contains the fix
for every mined PR, so absolute numbers on it are an upper bound; paired
arms over the same index are still comparable.

    python bench/code_intel_mine_prs.py --env-file .env \
        --namespace code-clickhouse --months 12 \
        --out bench/fixtures/code_intel_mined_prs.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

LINK_RE = re.compile(
    r"(?i)\b(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)\s*:?\s*#(\d+)"
)
REVERT_RE = re.compile(r"(?i)^\s*revert\b")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")


def _client(env_file: str | None) -> Any:
    env = dict(os.environ)
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                if v[:1] not in "{\"'" and " #" in v:
                    v = v.split(" #", 1)[0].rstrip()
                env.setdefault(k.strip(), v.strip("'").strip('"'))
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=env["LOOM_CH_HOST"],
        port=int(env.get("LOOM_CH_PORT", "8443")),
        username=env["LOOM_CH_USER"],
        password=env["LOOM_CH_PASSWORD"],
        database=env.get("LOOM_CH_DATABASE", "loom"),
        secure=env.get("LOOM_CH_SECURE", "true").lower() != "false",
        settings={"readonly": 1, "max_execution_time": 180},
    )


def _rows(ch: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    res = ch.query(sql, parameters=params)
    return [dict(zip(res.column_names, r, strict=False)) for r in res.result_rows]


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.split(".", 1)[0]


def _mechanism_prefix(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path


def mine(
    ch: Any, org: str, ns: str, months: int, min_files: int, max_files: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    prs = _rows(
        ch,
        """
        SELECT pr_number, title, body_excerpt, merged_at,
               arrayFilter(f -> startsWith(f, 'src/'), files) AS src_files
        FROM loom.code_pr_history FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND merged_at > now() - toIntervalMonth({months:UInt32})
        ORDER BY merged_at DESC
        """,
        {"org": org, "ns": ns, "months": months},
    )
    stats = {
        "prs": len(prs),
        "file_count_out_of_range": 0,
        "short_body": 0,
        "revert": 0,
        "issue_linked": 0,
        "issue_title_found": 0,
        "leak_dropped": 0,
        "kept": 0,
    }
    linked: dict[int, int] = {}
    for p in prs:
        m = LINK_RE.search(str(p.get("body_excerpt") or ""))
        if m:
            linked[int(p["pr_number"])] = int(m.group(1))
    titles: dict[int, str] = {}
    if linked:
        for r in _rows(
            ch,
            "SELECT number, title FROM loom.code_gh_items FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} "
            "AND is_pr = 0 AND number IN {nums:Array(UInt32)}",
            {"org": org, "ns": ns, "nums": sorted(set(linked.values()))},
        ):
            titles[int(r["number"])] = str(r["title"] or "")
    out: list[dict[str, Any]] = []
    for p in prs:
        files = [str(f) for f in (p.get("src_files") or [])]
        if not (min_files <= len(files) <= max_files):
            stats["file_count_out_of_range"] += 1
            continue
        if len(str(p.get("body_excerpt") or "")) <= 200:
            stats["short_body"] += 1
            continue
        title = str(p.get("title") or "").strip()
        if REVERT_RE.match(title):
            stats["revert"] += 1
            continue
        pr = int(p["pr_number"])
        issue = linked.get(pr)
        lane = "pr_title"
        query = title
        if issue is not None:
            stats["issue_linked"] += 1
            if titles.get(issue):
                stats["issue_title_found"] += 1
                query, lane = titles[issue], "issue"
        if len(query) < 12:
            continue
        stems = {_stem(f).lower() for f in files}
        words = {w.lower() for w in WORD_RE.findall(query)}
        if stems & words:
            stats["leak_dropped"] += 1
            continue
        out.append(
            {
                "id": f"pr{pr}",
                "type": "conceptual",
                "query": query,
                "gold": {"paths": sorted({_mechanism_prefix(f) for f in files})},
                "lane": lane,
                "source": {"pr_number": pr, "issue": issue, "merged_at": str(p.get("merged_at"))},
            }
        )
        stats["kept"] += 1
    return out, stats


# ---- history lane ------------------------------------------------
# Questions ABOUT a change, built from the same merged PRs: an explicit
# number ("what fixed issue N", "what changed in PR N") or a phrasing that
# names the change ("which PR fixed <issue title>", "when was the change
# that <PR title paraphrase> merged"). Gold = the PR itself and its `src/`
# files. The same leak filter applies to the phrase forms; number forms
# carry no words to leak.

_NUMBER_ISSUE_FORMS = (
    "what fixed issue {n}",
    "which PR closed #{n}",
    "fix status of issue {n}",
    "What do we know about GitHub issue {n}?",
)
_NUMBER_PR_FORMS = (
    "what changed in PR {n}",
    "which files did PR #{n} touch",
    "What is the fix status of pull request {n}?",
)
_PHRASE_ISSUE_FORMS = (
    "what fixed {t}",
    "when was {t} fixed",
    "which PR fixed {t}",
)
_PHRASE_PR_FORMS = (
    "which PR {t}",
    "when was the change that {t} merged",
    "who changed the code so that it {t}",
)
_VERB_PARAPHRASE = (
    ("fix ", "fixed "),
    ("fixes ", "fixed "),
    ("fixed ", "fixed "),
    ("add ", "added "),
    ("adds ", "added "),
    ("added ", "added "),
    ("support ", "added support for "),
    ("allow ", "allowed "),
    ("improve ", "improved "),
    ("better ", "improved "),
    ("remove ", "removed "),
    ("disable ", "disabled "),
    ("enable ", "enabled "),
    ("make ", "made "),
    ("use ", "switched to "),
    ("avoid ", "avoided "),
    ("do not ", "stopped "),
    ("don't ", "stopped "),
    ("update ", "updated "),
    ("refactor ", "refactored "),
    ("speed up ", "sped up "),
    ("optimize ", "optimized "),
)


def paraphrase_title(title: str) -> str:
    """A PR title as the clause of "which PR ...": leading conventional verb
    conjugated, trailing period dropped. "Fix detach for postgres tables" ->
    "fixed detach for postgres tables"; a title with no known leading verb
    reads "introduced <title>"."""
    t = title.strip().rstrip(".").strip()
    if not t:
        return ""
    low = t.lower()
    for verb, past in _VERB_PARAPHRASE:
        if low.startswith(verb):
            return past + t[len(verb) :]
    return "introduced " + t[0].lower() + t[1:]


def mine_history(
    ch: Any,
    org: str,
    ns: str,
    months: int,
    min_files: int,
    max_files: int,
    *,
    per_lane: int = 30,
    seed: int = 2860,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    import random

    rng = random.Random(seed)
    prs = _rows(
        ch,
        """
        SELECT pr_number, title, body_excerpt, merged_at, author,
               arrayFilter(f -> startsWith(f, 'src/'), files) AS src_files
        FROM loom.code_pr_history FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND merged_at > now() - toIntervalMonth({months:UInt32})
          AND NOT endsWith(author, '[bot]') AND NOT startsWith(author, 'robot-')
        ORDER BY merged_at DESC
        """,
        {"org": org, "ns": ns, "months": months},
    )
    stats = {
        "prs": len(prs),
        "file_count_out_of_range": 0,
        "revert": 0,
        "issue_linked": 0,
        "issue_title_found": 0,
        "leak_dropped": 0,
        "kept": 0,
    }
    linked: dict[int, int] = {}
    for p in prs:
        m = LINK_RE.search(str(p.get("body_excerpt") or ""))
        if m:
            linked[int(p["pr_number"])] = int(m.group(1))
    titles: dict[int, str] = {}
    if linked:
        for r in _rows(
            ch,
            "SELECT number, title FROM loom.code_gh_items FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} "
            "AND is_pr = 0 AND number IN {nums:Array(UInt32)}",
            {"org": org, "ns": ns, "nums": sorted(set(linked.values()))},
        ):
            titles[int(r["number"])] = str(r["title"] or "")
    lanes: dict[str, list[dict[str, Any]]] = {
        "number_issue": [],
        "number_pr": [],
        "phrase_issue": [],
        "phrase_pr": [],
    }

    def _leaks(query: str, files: list[str]) -> bool:
        stems = {_stem(f).lower() for f in files}
        words = {w.lower() for w in WORD_RE.findall(query)}
        return bool(stems & words)

    for p in prs:
        files = [str(f) for f in (p.get("src_files") or [])]
        if not (min_files <= len(files) <= max_files):
            stats["file_count_out_of_range"] += 1
            continue
        title = str(p.get("title") or "").strip()
        if REVERT_RE.match(title) or len(title) < 12:
            stats["revert"] += 1
            continue
        pr = int(p["pr_number"])
        issue = linked.get(pr)
        gold: dict[str, Any] = {
            "pr_number": pr,
            "paths": sorted({_mechanism_prefix(f) for f in files}),
        }
        source = {"pr_number": pr, "issue": issue, "merged_at": str(p.get("merged_at"))}
        if issue is not None:
            stats["issue_linked"] += 1
            gold["issue"] = issue
            lanes["number_issue"].append(
                {
                    "id": f"hn{pr}",
                    "type": "history",
                    "lane": "number_issue",
                    "query": rng.choice(_NUMBER_ISSUE_FORMS).format(n=issue),
                    "gold": gold,
                    "source": source,
                }
            )
            if titles.get(issue):
                stats["issue_title_found"] += 1
                t = titles[issue].strip().rstrip(".")
                q = rng.choice(_PHRASE_ISSUE_FORMS).format(t=t[0].lower() + t[1:])
                if _leaks(q, files):
                    stats["leak_dropped"] += 1
                else:
                    lanes["phrase_issue"].append(
                        {
                            "id": f"hpi{pr}",
                            "type": "history",
                            "lane": "phrase_issue",
                            "query": q,
                            "gold": gold,
                            "source": source,
                        }
                    )
        else:
            lanes["number_pr"].append(
                {
                    "id": f"hnp{pr}",
                    "type": "history",
                    "lane": "number_pr",
                    "query": rng.choice(_NUMBER_PR_FORMS).format(n=pr),
                    "gold": gold,
                    "source": source,
                }
            )
        q = rng.choice(_PHRASE_PR_FORMS).format(t=paraphrase_title(title))
        if _leaks(q, files):
            stats["leak_dropped"] += 1
        else:
            lanes["phrase_pr"].append(
                {
                    "id": f"hpp{pr}",
                    "type": "history",
                    "lane": "phrase_pr",
                    "query": q,
                    "gold": gold,
                    "source": source,
                }
            )
    out: list[dict[str, Any]] = []
    for lane, rows_ in lanes.items():
        rng.shuffle(rows_)
        take = rows_[: per_lane // 2 if lane == "number_pr" else per_lane]
        stats[f"lane_{lane}"] = len(take)
        out.extend(sorted(take, key=lambda r: r["id"]))
    stats["kept"] = len(out)
    return out, stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--org", default="dev")
    ap.add_argument("--namespace", default="code-clickhouse")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--min-files", type=int, default=1)
    ap.add_argument("--max-files", type=int, default=8)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--history",
        action="store_true",
        help="build the history lane: number / phrase questions "
        "about a change with gold pr_number + files",
    )
    ap.add_argument(
        "--per-lane",
        type=int,
        default=30,
        help="--history: questions per lane (number_pr takes half)",
    )
    args = ap.parse_args()
    if args.history:
        rows, stats = mine_history(
            _client(args.env_file),
            args.org,
            args.namespace,
            args.months,
            args.min_files,
            args.max_files,
            per_lane=args.per_lane,
        )
        header = [
            "// code_intel_history — questions ABOUT a change, mined from",
            f"// merged PRs (bench/code_intel_mine_prs.py --history, {args.namespace}, last "
            f"{args.months} months).",
            "// Lanes: number_issue ('what fixed issue N'), number_pr ('what changed in PR N'),",
            "// phrase_issue ('which PR fixed <issue title>'), phrase_pr ('which PR <title",
            "// paraphrase>'). gold.pr_number = the PR; gold.paths = its changed src/ files,",
            "// extension stripped. The live index and ledger hold every PR: absolute",
            "// numbers are an upper bound, paired deltas are the measurement. Never pinned",
            "// (pr_leg_before): the question asks for the PR itself.",
            "// " + json.dumps(stats),
        ]
        Path(args.out).write_text(
            "\n".join(header)
            + "\n"
            + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
            + "\n"
        )
        print(json.dumps(stats), "->", args.out)
        return
    rows, stats = mine(
        _client(args.env_file),
        args.org,
        args.namespace,
        args.months,
        args.min_files,
        args.max_files,
    )
    header = [
        "// code_intel_mined_prs — issue-shaped localization lane mined from merged PRs",
        f"// (bench/code_intel_mine_prs.py, {args.namespace}, last {args.months} months).",
        "// Query = linked",
        "// issue title where the PR body links one (lane=issue), else the PR title",
        "// (lane=pr_title). gold.paths = changed src/ files, extension stripped.",
        "// UPPER BOUND: the live index contains every fix; compare paired arms on it,",
        "// do not quote its absolute recall as the plane's.",
        "// " + json.dumps(stats),
    ]
    Path(args.out).write_text(
        "\n".join(header) + "\n" + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    )
    print(json.dumps(stats), "->", args.out)


if __name__ == "__main__":
    main()
