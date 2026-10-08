"""Derive the keyword-agnostic (KA) slice of the mined localization lanes.

Lexical localizers are expected to collapse once keyword references are removed from
localization questions; loom's code.search legs are lexical (chunk lexemes, the history
vote, the kind prior), and lanes where the issue title shares lexemes with the fix
cannot show that weakness. This script reads the mined lanes and keeps the questions
whose `code_tokens(query)` — the SAME tokenizer the lexical leg indexes and queries with
— share NO lexeme with any gold chunk's `lexemes` column.

One read-only statement per lane against `loom.code_chunks`: for every gold
mechanism prefix, the union of the lexemes of the live chunks it covers
(the scorer's own prefix rule: `path = g`, `path LIKE g/%`, `path LIKE g.%`).
No writes, no scratch tables, no embeddings.

    python bench/code_intel_ka_slice.py --env-file .env \
        --issue bench/fixtures/code_intel_mined_issue.jsonl \
        --pr-title bench/fixtures/code_intel_mined_pr_title.jsonl \
        --min-issue 100 --out bench/fixtures/code_intel_mined_issue_ka.jsonl

Rule: the issue lane's KA subset is the slice; if it holds fewer than `--min-issue`
questions the pr_title lane's KA subset is appended; the total is the instrument (about
150 questions resolve a 3-point claim). Under 60 in total, the plane has no measurable
keyword-agnostic demand and the number is recorded instead of a bench run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from server.code.domain.extract.tokenize import code_tokens  # noqa: E402


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Fixture rows (the `//` header lines skipped)."""
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("//")
    ]


def gold_prefixes(rows: list[dict[str, Any]]) -> list[str]:
    out: set[str] = set()
    for r in rows:
        for g in (r.get("gold") or {}).get("paths") or []:
            out.add(str(g).rstrip("/"))
    return sorted(out)


def read_gold_lexemes(
    ch: Any, org: str, ns: str, prefixes: list[str], *, batch: int = 400
) -> dict[str, set[str]]:
    """prefix -> union of the lexemes of the live chunks under it. Plain
    WHERE; the prefix rule is the scorer's."""
    out: dict[str, set[str]] = {p: set() for p in prefixes}
    for i in range(0, len(prefixes), batch):
        chunk = prefixes[i : i + batch]
        res = ch.query(
            """
            SELECT g AS prefix,
                   arrayDistinct(arrayFlatten(groupArray(splitByChar(' ', lexemes)))) AS lex
            FROM loom.code_chunks FINAL
            ARRAY JOIN arrayFilter(
                p -> path = p OR startsWith(path, concat(p, '/'))
                     OR startsWith(path, concat(p, '.')),
                {prefixes:Array(String)}
            ) AS g
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND arrayExists(
                    p -> path = p OR startsWith(path, concat(p, '/'))
                         OR startsWith(path, concat(p, '.')),
                    {prefixes:Array(String)}
                  )
            GROUP BY g
            """,
            parameters={"org": org, "ns": ns, "prefixes": chunk},
        )
        for prefix, lex in res.result_rows:
            out[str(prefix)].update(str(t) for t in lex if t)
    return out


def read_common_lexemes(
    ch: Any, org: str, ns: str, tokens: list[str], *, df_above: float
) -> set[str]:
    """Lexemes present in at least `df_above` of the namespace's live chunks
    (`loom.code_term_freq`, the lexical leg's own df) — the robustness
    variant of the rule: a token every file carries ("the", "query",
    "table") is not a keyword the lexical leg can localize with."""
    if not tokens or df_above <= 0:
        return set()
    n_row = ch.query(
        """
        SELECT count() FROM loom.code_chunks FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
        """,
        parameters={"org": org, "ns": ns},
    ).result_rows
    n_chunks = int(n_row[0][0]) if n_row else 0
    if n_chunks <= 0:
        return set()
    res = ch.query(
        """
        SELECT term, uniqExactMerge(df_state) AS df FROM loom.code_term_freq
        WHERE org = {org:String} AND namespace = {ns:String} AND term IN {terms:Array(String)}
        GROUP BY term
        """,
        parameters={"org": org, "ns": ns, "terms": sorted(set(tokens))},
    )
    return {str(t) for t, df in res.result_rows if int(df) >= df_above * n_chunks}


def shared_lexemes(
    query: str,
    gold: list[str],
    lex_by_prefix: dict[str, set[str]],
    *,
    ignore: set[str] | None = None,
) -> set[str]:
    """Query lexemes that occur in ANY gold chunk's lexemes — the overlap
    the lexical legs can exploit. Empty = keyword-agnostic. `ignore` drops
    lexemes that do not count as keywords (the df variant)."""
    tokens = set(code_tokens(query)) - (ignore or set())
    union: set[str] = set()
    for g in gold:
        union |= lex_by_prefix.get(str(g).rstrip("/"), set())
    return tokens & union


def derive_slice(
    rows: list[dict[str, Any]],
    lex_by_prefix: dict[str, set[str]],
    *,
    ignore: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Rows whose query shares no lexeme with any gold chunk, with the
    overlap histogram every reading of the slice should carry. A gold set
    with NO indexed chunk at all is not keyword-agnostic — it is
    unanswerable — and is counted separately."""
    kept: list[dict[str, Any]] = []
    stats = {
        "rows": len(rows),
        "no_indexed_gold": 0,
        "ka": 0,
        "shared_1": 0,
        "shared_2_3": 0,
        "shared_4_plus": 0,
    }
    for r in rows:
        gold = [str(g) for g in (r.get("gold") or {}).get("paths") or []]
        if not any(lex_by_prefix.get(g.rstrip("/")) for g in gold):
            stats["no_indexed_gold"] += 1
            continue
        n = len(shared_lexemes(str(r.get("query") or ""), gold, lex_by_prefix, ignore=ignore))
        if n == 0:
            stats["ka"] += 1
            kept.append(dict(r))
        elif n == 1:
            stats["shared_1"] += 1
        elif n <= 3:
            stats["shared_2_3"] += 1
        else:
            stats["shared_4_plus"] += 1
    return kept, stats


def _client(env_file: str | None) -> Any:
    from code_intel_mine_prs import _client as mk

    return mk(env_file)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--org", default="dev")
    ap.add_argument("--namespace", default="code-clickhouse")
    ap.add_argument("--issue", default=str(_HERE / "fixtures" / "code_intel_mined_issue.jsonl"))
    ap.add_argument(
        "--pr-title", default=str(_HERE / "fixtures" / "code_intel_mined_pr_title.jsonl")
    )
    ap.add_argument(
        "--min-issue",
        type=int,
        default=100,
        help="below this many issue-lane KA questions, append the pr_title KA subset",
    )
    ap.add_argument(
        "--ignore-df-above",
        type=float,
        default=0.0,
        help="robustness variant: lexemes present in at least this fraction of "
        "the namespace's live chunks do not count as overlap (0 = strict rule)",
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    ch = _client(args.env_file)
    issue_rows = load_rows(Path(args.issue))
    lex = read_gold_lexemes(ch, args.org, args.namespace, gold_prefixes(issue_rows))

    def _ignore(rows: list[dict[str, Any]]) -> set[str]:
        toks = [t for r in rows for t in code_tokens(str(r.get("query") or ""))]
        return read_common_lexemes(
            ch, args.org, args.namespace, toks, df_above=args.ignore_df_above
        )

    kept, stats = derive_slice(issue_rows, lex, ignore=_ignore(issue_rows))
    header_stats: dict[str, Any] = {
        "rule": {"ignore_df_above": args.ignore_df_above},
        "issue": stats,
    }
    if len(kept) < args.min_issue and args.pr_title:
        pr_rows = load_rows(Path(args.pr_title))
        lex_pr = read_gold_lexemes(ch, args.org, args.namespace, gold_prefixes(pr_rows))
        kept_pr, stats_pr = derive_slice(pr_rows, lex_pr, ignore=_ignore(pr_rows))
        header_stats["pr_title"] = stats_pr
        kept.extend(kept_pr)
    header_stats["total_ka"] = len(kept)
    header = [
        "// code_intel_mined_issue_ka — keyword-agnostic slice of the mined lanes",
        f"// (bench/code_intel_ka_slice.py, {args.namespace}): questions whose",
        "// code_tokens(query) share NO lexeme with any gold chunk's `lexemes`",
        "// (the keyword-agnostic setting). Same gold, same pin, same upper-bound",
        "// caveat as the parent lanes; `lane` is the parent lane, kept so the",
        "// runner's --lane filter and the pin still apply.",
        "// " + json.dumps(header_stats),
    ]
    Path(args.out).write_text(
        "\n".join(header) + "\n" + "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n"
    )
    print(json.dumps(header_stats), "->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
