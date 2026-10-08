"""The window after relevance: history anchors, the fan-in order and floor, the
seen-set, per-symbol diversity and per-file saturation."""

from __future__ import annotations

import logging

from server.code.app import fan_in as code_fan_in
from server.code.app import history as code_history
from server.code.app.search.priors import _TEST_INTENT_TOKENS
from server.code.app.search.state import _POOL_KEEP, _SearchState
from server.code.app.spans import code_span, span_attrs
from server.code.domain.extract.tokenize import code_tokens
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)


async def _order_window(
    repo: CodeRepo,
    org: str,
    ns: str,
    st: _SearchState,
    ranked: list[CodeUnit],
    *,
    query: str,
    top_k: int,
    path_prefix: str,
    sort_by: str,
    min_fan_in: int,
    exclude_unit_ids: list[str] | None,
) -> list[CodeUnit]:
    """After relevance: history anchors, the caller's fan-in ordering and
    floor, the seen-set, per-symbol diversity (sets `result.pool`) and the
    per-file saturation; returns the ordered list the page is cut from."""
    result, work, constrained = st.result, st.work, st.constrained
    history_units, history_section = st.history_units, st.history_section
    fan_in_explain = st.fan_in_explain
    # ---- history anchors: the section's PR files rise through the lift
    # shape — a file already in the window stays where the ranking put it, an absent one
    # enters at rank 1, nothing is dropped. Applied AFTER every relevance prior (the
    # kind prior would discount a lifted class skeleton back below the fused units) and
    # before the caller's own ordering (sort_by) and the diversity pass.
    if history_units:
        ranked, history_section["anchored"] = code_history.apply_file_anchors(
            ranked, history_units, top_k
        )

    # ---- dependency order: sort_by='fan_in' ranks the pool
    # by how much of the repo calls each unit and, under a path_prefix, adds the
    # prefix's most-called symbols as candidates so "which symbol under src/X has the
    # most callers" is one call. min_fan_in drops units below the floor. Both run after
    # every relevance signal and before the per-symbol diversity pass; abstention was
    # decided above and is untouched.
    hub_added = 0
    # hub candidates come from the symbol stats, not the predicate: off under a constraint
    if sort_by == "fan_in" and path_prefix and not constrained:
        try:
            with code_span("search.fan_in_hubs") as sp:
                hubs = await code_fan_in.hub_units(repo, org, ns, path_prefix, limit=max(top_k, 5))
                ranked, hub_added = code_fan_in.merge_hubs(ranked, hubs)
                span_attrs(sp, results=hub_added)
        except Exception as e:  # noqa: BLE001 — the pool still sorts
            logger.debug("fan-in hub candidates skipped: %s", e)
    if min_fan_in > 0:
        ranked = [u for u in ranked if u.fan_in >= min_fan_in]
    if sort_by == "fan_in":
        ranked = code_fan_in.order_by_fan_in(ranked)
        fan_in_explain = (
            (fan_in_explain or "fan_in=0")
            + ", sort_by=fan_in"
            + (f"(hub_candidates={hub_added})" if hub_added else "")
        )
        # A hub unit in the served page is the materialised answer to "what
        # does the repo call most under this prefix", not a retrieval guess:
        # the abstention verdict on the pool does not apply to it. Disclosed
        # in the calibration text; the risk grade of the pool itself stays.
        if result.abstain and any(u.score == 0 and u.fan_in for u in ranked[:top_k]):
            result.abstain, result.abstain_code = False, ""
            result.abstain_reason = ""
            result.calibration_explanation = (
                (result.calibration_explanation + "; " if result.calibration_explanation else "")
                + f"pool would abstain; served the most-called symbols under {path_prefix} "
                "(materialised fan-in, not a retrieval match)"
            )
            if result.retrieval_risk == "abstain":
                result.retrieval_risk = "medium"

    # ---- diversity: one slot per (path, scope). Oversized functions split
    # into several chunks that rank adjacently and can fill EVERY result
    # slot with the same symbol (observed live: five copies of
    # MergeTreeData::checkAlterIsPossible as the entire top-5). Keep the
    # best-ranked part per unit; leftovers refill trailing slots only if
    # distinct units run out.
    if exclude_unit_ids:
        ranked = _exclude(ranked, exclude_unit_ids, work)
    ranked = _diversify(ranked, top_k)
    result.pool = ranked[:_POOL_KEEP]

    # ---- per-file saturation (code_search_file_saturation; 0 = off): the 2nd, 3rd...
    # unit from one path decays by decay^excess so a single file cannot fill the window
    # (10.6% of hybrid searches returned every hit from one file). The file's BEST unit
    # is never touched — coherence above lifted it, saturation only spreads the slots
    # behind it — so the top slot cannot move. Counted before the cut whether or not it
    # runs, so an off arm still reports how often the window was saturable.
    work["same_path_in_topk"] = _same_path_in_window(ranked, top_k)
    decay = float(settings.code_search_file_saturation)
    saturation_explain = ""
    if 0.0 < decay < 1.0:
        skip = _saturation_skip_reason(route=result.route, path_prefix=path_prefix, query=query)
        if skip:
            saturation_explain = f"saturation=skipped({skip})"
        else:
            ranked, moved = _saturate_files(ranked, decay, top_k)
            work["saturation_moved"] = moved
            saturation_explain = f"saturation={decay:g}"

    st.fan_in_explain, st.saturation_explain = fan_in_explain, saturation_explain
    return ranked


def _exclude(units: list[CodeUnit], ids: list[str], work: dict[str, int]) -> list[CodeUnit]:
    """Drop the caller's seen-set (app-side, after fusion). Counted in
    retrieval_work.excluded_units so the trace shows how much a repeat
    query was pruned."""
    seen = set(ids)
    kept = [u for u in units if u.unit_id not in seen]
    work["excluded_units"] = work.get("excluded_units", 0) + (len(units) - len(kept))
    return kept


def _diversify(ranked: list[CodeUnit], top_k: int) -> list[CodeUnit]:
    seen: set[tuple[str, str]] = set()
    unique: list[CodeUnit] = []
    dupes: list[CodeUnit] = []
    for u in ranked:
        key = (u.path, u.symbol or f"#{u.start_line}")
        (dupes if key in seen else unique).append(u)
        seen.add(key)
    if len(unique) >= top_k:
        return unique
    return unique + dupes


def _has_test_intent(query: str) -> bool:
    return bool(frozenset(code_tokens(query)) & _TEST_INTENT_TOKENS)


def _same_path_in_window(ranked: list[CodeUnit], top_k: int) -> int:
    """Units in the top-k window that share a path with an earlier unit in
    it — the slots saturation would touch. 0 = the window is already one
    unit per file and saturation cannot change it."""
    seen: set[str] = set()
    n = 0
    for u in ranked[: max(top_k, 0)]:
        if u.path in seen:
            n += 1
        seen.add(u.path)
    return n


def _saturation_skip_reason(*, route: str, path_prefix: str, query: str) -> str:
    """Where several units of one file ARE the answer, saturation is off:
    a path_prefix scopes the search to a subtree the asker chose; a
    symbol-shaped query asks for one identifier's definition and uses,
    which live together; a test-intent query wants the cases of one test
    file."""
    if path_prefix:
        return "path_prefix"
    if route.startswith("symbol"):
        return "symbol"
    if _has_test_intent(query):
        return "test_intent"
    return ""


def _saturate_files(ranked: list[CodeUnit], decay: float, top_k: int) -> tuple[list[CodeUnit], int]:
    """Multiply the 2nd, 3rd... unit of each path by decay^excess and re-sort (stable,
    so equal scores keep fusion order). Multiplicative and only ever downward on a
    file's non-best units: relevance still decides (additive terms outgrew the usable
    score band), the best unit per file keeps the coherence lift, and the top slot never
    moves. Returns the new order and how many of the top-k slots changed occupant."""
    seen: dict[str, int] = {}
    out: list[CodeUnit] = []
    for u in ranked:
        excess = seen.get(u.path, 0)
        seen[u.path] = excess + 1
        if excess:
            out.append(u.model_copy(update={"score": round(u.score * decay**excess, 6)}))
        else:
            out.append(u)
    out.sort(key=lambda u: -u.score)
    before = [(u.path, u.start_line, u.unit_id) for u in ranked[:top_k]]
    after = [(u.path, u.start_line, u.unit_id) for u in out[:top_k]]
    moved = sum(1 for a, b in zip(before, after, strict=False) if a != b)
    return out, moved
