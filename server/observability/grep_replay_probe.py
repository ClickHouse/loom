"""Wrong-abstain gate for code.grep.

A code.grep abstain is *wrong* when the literal the caller sent is present
in the live chunks of the scope it asked about — the namespace, narrowed to
`path_prefix` when the call carried one: the statement
`position(content, pattern) > 0 [AND startsWith(path, prefix)]` should have
matched, and the caller was told "no match".

A literal can be present somewhere in the namespace and absent under the
caller's `path_prefix`, so a recount that ignores the scope calls a correct
abstain wrong; this one recounts inside it.

Two statements, both read-only, neither touches `system.query_log`:

1. the ledger — every literal-grep abstain of the trailing window from
   `loom.code_search_trace`, grouped per (org, namespace, consumer, pattern,
   path_prefix) with its call count; a row whose `path_prefix` is '' is
   recounted over the whole namespace;
2. the recount — ONE scan of the namespace's live chunks that counts, for
   every (pattern, prefix) pair at once, the chunks containing the pattern
   under the prefix (`multiSearchAllPositions` takes at most 255 needles, so
   pairs are batched; ≤ 255 pairs is one scan).

`wrong_abstain = abstain ∧ corpus_in_scope > 0`, counted in CALLS per
consumer. The fold is pure so the gauge, the bench and the tests share it.
The bench (`bench/code_grep_replay.py`) adds a replay against a server and
per-call detail; the gauge `loom.code.grep.wrong_abstain_24h` runs this
module on its own slow clock (`LOOM_OTEL_CODE_GREP_REPLAY_CACHE_S`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

log = logging.getLogger(__name__)

# `multiSearchAllPositions` accepts at most this many needles per call.
MULTISEARCH_MAX_NEEDLES = 255

# Only the two literal routes, with or without the `+prefix` marker: a regex
# route is not a literal recount, a `+scoped` route carries glob/language
# filters the recount does not apply, a `+page` row is a point read of an
# earlier extraction.
_LEDGER_WHERE = """
FROM loom.code_search_trace
WHERE ts > now() - INTERVAL {hours:UInt32} HOUR
  AND op = 'code.grep' AND abstain = 1
  AND route IN ('grep_literal', 'grep_literal_files',
                'grep_literal+prefix', 'grep_literal+prefix_files')
  AND length(query) >= 2
"""

LEDGER_SQL = (
    "SELECT org, namespace, consumer, query AS pattern, path_prefix, count() AS calls"
    + _LEDGER_WHERE
    + "GROUP BY org, namespace, consumer, pattern, path_prefix\nORDER BY calls DESC, pattern\n"
)

# One pass over the namespace's live chunks: hits[i] = chunks under
# prefixes[i] ('' = any path) containing pats[i]. sumForEach over no rows is
# an empty array — the caller pads.
RECOUNT_SQL = """
SELECT sumForEach(arrayMap((hit, pre) -> toUInt32(hit > 0 AND startsWith(path, pre)),
                           multiSearchAllPositions(content, {pats:Array(String)}),
                           {prefixes:Array(String)})) AS hits,
       count() AS chunks
FROM loom.code_chunks FINAL
WHERE org = {org:String} AND namespace = {ns:String}
  AND tombstoned_at IS NULL
"""

Pair = tuple[str, str]  # (pattern, path_prefix)


def batches(pairs: Sequence[Pair], size: int = MULTISEARCH_MAX_NEEDLES) -> list[list[Pair]]:
    """Distinct (pattern, prefix) pairs, order kept, in slices of at most `size`."""
    seen: dict[Pair, None] = dict.fromkeys((str(p), str(pre or "")) for p, pre in pairs if p)
    uniq = list(seen)
    return [uniq[i : i + size] for i in range(0, len(uniq), size)]


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in getattr(result, "result_rows", []) or []]


def recount(client: Any, org: str, ns: str, pairs: Sequence[Pair]) -> dict[Pair, int]:
    """{(pattern, prefix): live chunks under prefix containing pattern} for
    one namespace, one scan per 255 distinct pairs. `client` is a sync
    clickhouse-connect client (the probe's) — `query(sql, parameters=...)`."""
    out: dict[Pair, int] = {}
    for batch in batches(pairs):
        params = {
            "org": org,
            "ns": ns,
            "pats": [p for p, _ in batch],
            "prefixes": [pre for _, pre in batch],
        }
        rows = _rows(client.query(RECOUNT_SQL, parameters=params))
        hits = list(rows[0][0]) if rows and rows[0] and rows[0][0] else []
        hits += [0] * (len(batch) - len(hits))
        out.update(zip(batch, (int(h) for h in hits), strict=False))
    return out


def ledger_abstains(client: Any, hours: int = 24) -> list[tuple[str, str, str, str, str, int]]:
    """(org, namespace, consumer, pattern, path_prefix, calls) rows."""
    rows = _rows(client.query(LEDGER_SQL, parameters={"hours": int(hours)}))
    return [(str(o), str(n), str(c), str(p), str(pre or ""), int(k)) for o, n, c, p, pre, k in rows]


def fold(
    abstains: Sequence[tuple[str, str, str, str, str, int]],
    corpus: dict[tuple[str, str], dict[Pair, int]],
) -> list[dict[str, Any]]:
    """Pure: per (org, namespace, consumer) — abstained calls, wrong calls
    (pattern present in the corpus under the call's prefix), and the wrong
    patterns with their counts. A consumer with abstains and no wrong ones
    reads 0, so the series does not vanish when the defect stops."""
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for org, ns, consumer, pattern, prefix, calls in abstains:
        row = by_key.setdefault(
            (org, ns, consumer),
            {
                "org": org,
                "namespace": ns,
                "consumer": consumer,
                "abstain_calls": 0,
                "wrong_calls": 0,
                "wrong_patterns": [],
            },
        )
        row["abstain_calls"] += int(calls)
        hits = int(corpus.get((org, ns), {}).get((pattern, prefix or ""), 0))
        if hits > 0:
            row["wrong_calls"] += int(calls)
            row["wrong_patterns"].append(
                {
                    "pattern": pattern,
                    "path_prefix": prefix or "",
                    "calls": int(calls),
                    "corpus": hits,
                }
            )
    for row in by_key.values():
        row["wrong_patterns"].sort(key=lambda d: (-d["corpus"], d["pattern"], d["path_prefix"]))
    return sorted(by_key.values(), key=lambda r: (r["org"], r["namespace"], r["consumer"]))


# `consumer` is the client's own word unless a token claim bound it (any
# client can send any tag, and code_search_trace does not record which), so
# the gauge keeps the SERIES_MAX_CONSUMERS worst consumers per (org, namespace)
# as their own label value and sums the rest under OTHER_CONSUMER: at most
# SERIES_MAX_CONSUMERS + 1 series per namespace, whatever the clients send.
SERIES_MAX_CONSUMERS = 8
OTHER_CONSUMER = "other"


def _capped(folded: Sequence[dict[str, Any]]) -> list[tuple[str, str, str, int]]:
    """(org, namespace, consumer label, wrong calls), the tail per namespace
    folded into OTHER_CONSUMER. Rank: wrong calls, then abstains, then name."""
    by_ns: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in folded:
        by_ns.setdefault((str(r["org"]), str(r["namespace"])), []).append(r)
    out: list[tuple[str, str, str, int]] = []
    for (org, ns), rows in by_ns.items():
        ranked = sorted(
            rows, key=lambda r: (-int(r["wrong_calls"]), -int(r["abstain_calls"]), r["consumer"])
        )
        head, tail = ranked[:SERIES_MAX_CONSUMERS], ranked[SERIES_MAX_CONSUMERS:]
        out += [(org, ns, str(r["consumer"]), int(r["wrong_calls"])) for r in head]
        if tail:
            out.append((org, ns, OTHER_CONSUMER, sum(int(r["wrong_calls"]) for r in tail)))
    return out


def series(folded: Sequence[dict[str, Any]]) -> dict[str, list[tuple[float, dict[str, str]]]]:
    """The gauge series: wrong abstained calls per (org, namespace, consumer),
    at most SERIES_MAX_CONSUMERS + 1 consumer values per namespace."""
    return {
        "code_grep_wrong_abstain_24h": [
            (float(wrong), {"loom.org": org, "loom.namespace": ns, "loom.consumer": consumer})
            for org, ns, consumer, wrong in _capped(folded)
        ]
    }


def wrong_abstains(client: Any, hours: int = 24) -> list[dict[str, Any]]:
    """Ledger + one recount per namespace with abstains → the folded table."""
    abstains = ledger_abstains(client, hours)
    by_ns: dict[tuple[str, str], list[Pair]] = {}
    for org, ns, _consumer, pattern, prefix, _calls in abstains:
        by_ns.setdefault((org, ns), []).append((pattern, prefix))
    corpus = {key: recount(client, key[0], key[1], pairs) for key, pairs in by_ns.items()}
    return fold(abstains, corpus)


def probe(client: Any, hours: int = 24) -> dict[str, list[tuple[float, dict[str, str]]]]:
    """What the code-state probe calls: the series, or {} when the ledger or
    the recount cannot be read (a refused grant, a table that is not there)
    — the other code gauges keep exporting."""
    try:
        return series(wrong_abstains(client, hours))
    except Exception as e:  # noqa: BLE001 — a gauge probe never breaks the export
        log.debug("code.grep wrong-abstain probe skipped: %s", e)
        return {}
