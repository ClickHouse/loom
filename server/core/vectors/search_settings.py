"""The `SETTINGS` fragment every vector-ranked read carries, for every plane
(memory, code, knowledge, tracker)."""

from __future__ import annotations

from server.core.vectors.quantized_setting import quantized_codes_supported


def vector_search_settings(
    *,
    fetch_multiplier: float | None = None,
    quantized: bool | None = None,
    table: str | None = None,
    column: str = "embedding",
) -> str:
    """The `SETTINGS ...` fragment for a vector-ranked read, or "" when the read
    is a plain exact scan.

    No table has a vector-similarity index, so the only setting a vector read
    needs is the codes-first read: `vector_search_use_quantized_codes = 1` with
    its shortlist size, `vector_search_index_fetch_multiplier`. Callers append
    this to a query that already ends in `ORDER BY <bare cosineDistance(col,
    ref)> ASC LIMIT n` — see the note below on why that shape is not negotiable.

    `quantized`: None = the deployment default (the setting and the replica
    probe); False keeps the codes flag out of this statement whatever the
    deployment says (the main fact leg while its table carries patch parts);
    True still needs the probe — an unknown setting name
    fails the whole query, so a caller cannot force it onto a 26.6 replica.

    `table` / `column`: the vector the statement ranks on.
    With them, the deployment default also requires that column to carry a
    `Quantized(...)` codec and the table to be free of patch parts
    (`vector_codes_state.codes_apply`) — the two conditions under which the
    flag reads less, not the same or more. Every call site names them; the
    bare form is the deployment-wide answer.

    `fetch_multiplier`: the shortlist is LIMIT x this; None reads
    `vector_search_quantized_fetch_multiplier`.
    """
    from server.settings import settings

    deployment = bool(settings.vector_search_quantized_codes and quantized_codes_supported())
    if quantized is None:
        quantized = deployment
        if quantized and table is not None:
            from server.core.vectors.codes_state import codes_apply

            quantized = codes_apply(table, column)
    elif quantized:
        quantized = quantized_codes_supported()
    if not quantized:
        return ""
    if fetch_multiplier is None:
        # Measured on synthetic 1536-dim vectors: x3 at LIMIT 10 kept 0.73-0.88
        # of the exact top 10 on 1M- and 500k-row reads; the fact leg's LIMIT 60
        # at x1 kept 0.95-1.00.
        fetch_multiplier = settings.vector_search_quantized_fetch_multiplier
    # With a `Quantized(...)` codec on the column this turns the scan into
    # codes-first + full-precision rescore of LIMIT x fetch_multiplier
    # candidates; without a codec it is a no-op (measured 95 vs 96 ms,
    # identical rows). The name only exists on CH >= 26.7 and an unknown
    # setting fails the whole query, hence the boot probe. The codes flag comes
    # last: a replica that rejects it gets the query back without it
    # (`ch_errors.strip_quantized_setting`).
    return (
        f"SETTINGS vector_search_index_fetch_multiplier = {float(fetch_multiplier)}, "
        "vector_search_use_quantized_codes = 1"
    )


# Why every vector-ranked query in this codebase orders by a BARE distance
# expression, ASC, as the ONLY sort key — and reconstructs similarity in
# Python instead of in SQL.
# CH's vector-search optimizations (both the `vector_similarity` index path and
# the newer `Quantized(...)` two-stage codec path) pattern-match a rigid query
# plan: LimitStep -> SortingStep -> ExpressionStep -> [FilterStep] ->
# ReadFromMergeTree. They bail out when
#   - the sort has more than one key          (`ORDER BY d ASC, ts DESC`)
#   - the sort key is not a bare distance call (`1.0 - cosineDistance(...)`)
#   - the direction is DESC for cosineDistance/L2Distance
#   - anything else sits between LIMIT and the read: `LIMIT n BY x`, a window
#     function, a GROUP BY, a subquery boundary
# so a query written any other way is permanently brute-force no matter what
# index or codec the table carries. `1.0 - cosineDistance(...) AS sim ... ORDER
# BY sim DESC` fails on two counts at once.
