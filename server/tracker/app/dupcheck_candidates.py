"""The duplicate check's candidates: tracker.similar's legs side by side
over the draft, plus the fingerprint, fix-path and fix-in-flight legs, fused
and hydrated into the rows the fit scores.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from server.settings import settings
from server.tracker.api.models import (
    TrackerSimilarInput,
)
from server.tracker.app import dupcheck_legs as dl
from server.tracker.app import similar as tsim
from server.tracker.domain import dupcheck as dc
from server.tracker.domain.errors import TrackerUnavailable

logger = logging.getLogger(__name__)


LEGS_ON = {"lexical", "fields", "frames", "mentions", "vector"}


# how deep into the fused list the fit scores (the answer still carries top_k)
SCORE_DEPTH = 25
# rows of a recall-only leg scored whatever their fused rank
LEG_HEAD = 5


def _max_length(field: Any) -> int:
    """A pydantic field's `max_length` constraint."""
    return next(int(m.max_length) for m in field.metadata if getattr(m, "max_length", None))


async def candidates(
    src: Any,
    input: Any,
    *,
    wall: datetime | None,
    status: dict[str, str],
) -> dict[str, Any]:
    """The draft through `tracker.similar`'s query builder and legs (title first line, body
    after it, the caller's frames and symbols, component, version and mentions folded into
    the query; with `issue_number`, the filed item's own text when the draft is empty, the
    item left out, its recorded relations read beside the legs), fused, hydrated, and
    enriched with the fix PRs that touched the draft's paths."""
    from server.tracker.domain import similar as sim

    text = "\n".join(p for p in (str(input.title or "").strip(), str(input.body or "")) if p)
    # The draft's own limits (title 500 + body 20000, frames 60 + symbols 40) are wider
    # than `similar`'s, so the joined text and the frame list are cut to its limits:
    # a draft at its own maximum is a valid request, never a 500.
    fields = TrackerSimilarInput.model_fields
    sim_input = TrackerSimilarInput(
        org=input.org,
        namespace=input.namespace,
        text=text[: _max_length(fields["text"])],
        frames=[*(input.frames or []), *(input.symbols or [])][: _max_length(fields["frames"])],
        kinds=["issue"],
        top_k=int(input.top_k),
        issue_number=int(input.issue_number or 0),
    )
    t_start = time.monotonic()
    q, stats, item = await tsim._similar_query(src, sim_input, sim)
    _shape_query(q, input)
    meta: dict[str, Any] = {}
    await _resolve_error_name(src, input, q, meta)
    draft_paths = _draft_paths(q, input)
    errors: dict[str, BaseException] = {}
    lists, record, in_flight, fingerprint = await asyncio.gather(
        tsim._similar_legs(
            src,
            sim_input,
            q,
            stats,
            item,
            ["issue"],
            set(LEGS_ON),
            status,
            meta,
            before=wall,
            errors=errors,
        ),
        tsim._similar_record(src, sim_input, q, item),
        _fix_in_flight_leg(
            src, input.org, input.namespace, draft_paths, q, wall, status, meta, errors=errors
        ),
        _fingerprint_leg(src, input, q, wall, meta),
    )
    # A leg that failed for a transient reason runs once more inside the op's budget: one
    # lexical timeout must not turn the answer into `none`.
    retry = _LegRetry(src, sim_input, q, stats, item, wall, draft_paths)
    trace, retried_flight = await retry.run(status, meta, errors, lists, until=dl.deadline(t_start))
    if retried_flight is not None:
        in_flight = retried_flight
    if in_flight:
        lists["fix_path"] = in_flight
    # what each leg put first, so a `none` is auditable after the fact
    meta["legs_top"] = {
        leg: [int(r.get("number") or 0) for r in rows[:5]] for leg, rows in lists.items() if rows
    }
    fused = sim.fuse(lists)
    # The fit scores the fused top SCORE_DEPTH, not the caller's top_k: a candidate only the
    # vector leg found sits after every lexical candidate and a top_k of 5 or 10 would cut it
    # off unscored.
    head = fused[: max(int(input.top_k), SCORE_DEPTH)]
    # A candidate only a recall-only leg found sits after every
    # ranked row (fifty of them when the lexical leg is full), past any depth;
    # the vector leg's top rows and every fix-in-flight row are scored too
    in_head = {str(r.get("source_id") or "") for r in head}
    for leg_rows in (
        (lists.get("vector") or [])[:LEG_HEAD],
        in_flight or [],
    ):
        for r in leg_rows:
            sid = str(r.get("source_id") or "")
            fused_row = next((f for f in fused if str(f.get("source_id") or "") == sid), r)
            if sid and sid not in in_head:
                head.append(fused_row)
                in_head.add(sid)
    # the fused row is the first leg's: put the fix-in-flight leg's PRs (and
    # their state) back on a candidate another leg found too
    flight_by = {str(r.get("source_id") or ""): r for r in in_flight or []}
    for r in head:
        src_row = flight_by.get(str(r.get("source_id") or ""))
        if src_row is not None and src_row is not r:
            r.setdefault("fix_paths", {}).update(src_row.get("fix_paths") or {})
            r.setdefault("fix_pr_state", {}).update(src_row.get("fix_pr_state") or {})
    # the fused row is the first leg's; the frames leg's hit vector rides along
    # for the evidence line ("3/5 frames match")
    hits = {str(r.get("source_id") or ""): r.get("frame_hits") for r in lists.get("frames") or []}
    for r in head:
        if r.get("frame_hits") is None and hits.get(str(r.get("source_id") or "")) is not None:
            r["frame_hits"] = hits[str(r.get("source_id") or "")]
    t0 = time.monotonic()
    ranked = await tsim._hydrate(src, sim_input, head, status)
    if any(not r.get("title") for r in head):
        meta.setdefault("timings_ms", {})["hydrate"] = int((time.monotonic() - t0) * 1000)
    if draft_paths and ranked:
        t1 = time.monotonic()
        await _fix_paths(src, input.org, input.namespace, ranked, draft_paths, status, errors)
        meta.setdefault("timings_ms", {})["fix_paths"] = int((time.monotonic() - t1) * 1000)
    return {
        "q": q,
        "item": item,
        "ranked": ranked,
        "record": record,
        "meta": meta,
        "draft_paths": draft_paths,
        # each leg's own head, for the rerank pool
        "leg_heads": {
            leg: [int(r.get("number") or 0) for r in rows[: _pool_head()]]
            for leg, rows in lists.items()
            if rows
        },
        "fingerprint": fingerprint,
        "leg_status": dl.leg_report(
            status,
            meta.get("timings_ms") or {},
            trace,
            errors,
            namespace=str(input.namespace or ""),
        ),
    }


# A code shared by more issues than this is no fingerprint
FINGERPRINT_MAX = 60


async def _resolve_error_name(src: Any, input: Any, q: Any, meta: dict[str, Any]) -> None:
    """A draft that quotes a server error by NAME and no `Code: N` gets the code the record
    pairs with that name, so the `error_code` evidence the fit weighs fires as it does for
    `Code: N` (e.g. "… (SIZES_OF_COLUMNS_DOESNT_MATCH) …" matches issues carrying code 9).
    The first name the record knows wins; any failure leaves the query as it was."""
    from server.tracker.domain import similar as sim

    if q.error_code:
        return
    text = "\n".join(p for p in (str(input.title or ""), str(input.body or "")) if p)
    names = sim.error_names_of(text)
    if not names:
        return
    try:
        codes = await src.error_name_codes(input.org, input.namespace)
    except Exception as e:  # noqa: BLE001 — the name only adds evidence
        logger.info("duplicate_check: error names unavailable (%s)", e)
        return
    for name in names:
        if codes.get(name):
            q.error_code = int(codes[name])
            meta["error_code_from"] = name
            return


async def _fingerprint_leg(
    src: Any, input: Any, q: Any, wall: datetime | None, meta: dict[str, Any]
) -> list[int]:
    """The issues sharing the draft's STID, or its error code when that code is rare, for the
    rerank pool only (never fused: the fit was calibrated on the other legs). [] on any
    failure."""
    from server.tracker.domain import similar as sim

    if not bool(settings.tracker_dupcheck_rerank_enabled) or not getattr(input, "rerank", True):
        return []
    text = "\n".join(p for p in (str(input.title or ""), str(input.body or "")) if p)
    stid = sim.stid_of(text)
    code = int(getattr(q, "error_code", 0) or 0)
    if not stid and code <= 0:
        return []
    try:
        rows = await src.fingerprint_issues(
            input.org,
            input.namespace,
            error_code=code,
            stid=stid,
            before=wall,
            limit=FINGERPRINT_MAX,
        )
    except Exception as e:  # noqa: BLE001 — the leg is skipped
        logger.info("duplicate_check: fingerprint leg skipped (%s)", e)
        return []
    own = int(getattr(q, "number", 0) or 0)
    meta["fingerprint"] = {"stid": stid, "error_code": code, "rows": len(rows)}
    return [n for n in rows if n != own]


# Rows kept per leg for the rerank pool (the setting takes fewer)
POOL_HEAD = 30


def pool_cap() -> int:
    """Candidates one rerank call reads, over every namespace of a fan-out (the setting;
    `dup_rerank.POOL_CAP` when unset)."""
    from server.tracker.app import dup_rerank as dr

    cap = int(settings.tracker_dupcheck_rerank_pool_cap)
    return cap if cap > 0 else dr.POOL_CAP


def _pool_head() -> int:
    """The rows kept per leg follow the rerank's per-leg depth when that is deeper than the
    default head."""
    per = int(settings.tracker_dupcheck_rerank_per_leg)
    return max(POOL_HEAD, per)


def _shape_query(q: Any, input: Any) -> None:
    if input.component:
        q.component = str(input.component)
    if input.version:
        q.versions = list(dict.fromkeys([*q.versions, dc.minor_version(str(input.version))]))
    extra = [int(n) for n in (input.mentions or []) if int(n) > 0]
    q.mentions = [n for n in dict.fromkeys([*q.mentions, *extra]) if n != q.number][:10]


def _draft_paths(q: Any, input: Any) -> list[str]:
    """The paths a fix may have touched: the caller's `paths` and the file
    frames of the trace."""
    paths = [str(p).strip() for p in (input.paths or []) if str(p).strip()]
    paths += [f.path for f in q.frames if getattr(f, "kind", "") == "file" and f.path]
    return list(dict.fromkeys(paths))[:40]


async def _fix_paths(
    src: Any,
    org: str,
    ns: str,
    ranked: list[dict[str, Any]],
    draft_paths: list[str],
    status: dict[str, str],
    errors: dict[str, BaseException] | None = None,
) -> None:
    """For the candidates in hand, the PRs the tracker graph says
    fixed them (one `fixes` edge read) and the paths those PRs touched (one
    file read, `pr_files`); `fix_path` on a row is the share of the
    draft's paths some fix touched, `fix_paths` names them per PR. An outage
    leaves the feature at 0 and says `tracker_unavailable` on the `fix_path` leg."""
    refs = [str(r.get("source_id") or "") for r in ranked if r.get("source_id")]
    try:
        edges = await src.edges_from(org, ns, refs, ["fixes"])
        prs_of: dict[str, list[int]] = {}
        for e in edges:
            other = int(str(e.get("dst_ref") or "").rsplit("#", 1)[-1] or 0)
            if other > 0:
                # `src_ref` is the graph's name for our end (`our_ref` is a fallback only; the
                # source does not return it)
                prs_of.setdefault(str(e.get("src_ref") or e.get("our_ref") or ""), []).append(other)
        wanted = sorted({n for ns_ in prs_of.values() for n in ns_})[:64]
        files = await src.pr_files(org, ns, wanted) if wanted else {}
    except TrackerUnavailable as e:
        logger.info("tracker.duplicate_check/fix_paths on %s: tracker_unavailable (%s)", ns, e)
        status["fix_path"] = "tracker_unavailable"
        if errors is not None:
            errors["fix_path"] = e
        return
    served = False
    for r in ranked:
        touched: dict[str, list[str]] = {}
        for pr in prs_of.get(str(r.get("source_id") or ""), []):
            hit = dc.path_overlap(draft_paths, files.get(pr, []))
            if hit:
                touched[str(pr)] = hit
        # the fix-in-flight leg already names the PRs that led here
        for pr, paths in (r.get("fix_paths") or {}).items():
            touched.setdefault(str(pr), list(paths))
        if touched:
            served = True
            covered = {p for paths in touched.values() for p in paths}
            r["fix_path"] = len(covered) / len(draft_paths)
            r["fix_paths"] = touched
            if "fix_path" not in (r.get("why") or []):
                r["why"] = [*(r.get("why") or []), "fix_path"]
    status["fix_path"] = "served" if served else status.get("fix_path", "empty")


class _LegRetry:
    """One more run of the legs whose read failed for a transient reason, after a short
    pause and only inside the op's budget; a deterministic failure (a refused statement, an
    unconfigured namespace, an open breaker) is not retried."""

    def __init__(
        self,
        src: Any,
        sim_input: Any,
        q: Any,
        stats: Any,
        item: Any,
        wall: datetime | None,
        draft_paths: list[str],
    ) -> None:
        self.src, self.sim_input, self.q, self.stats = src, sim_input, q, stats
        self.item, self.wall, self.draft_paths = item, wall, draft_paths

    async def run(
        self,
        status: dict[str, str],
        meta: dict[str, Any],
        errors: dict[str, BaseException],
        lists: dict[str, list[dict[str, Any]]],
        *,
        until: float,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]] | None]:
        """The trace per failed leg ({failure, retried, retry_ms, skipped}) and the
        fix-in-flight rows when that leg was retried (None otherwise); `status`, `lists`
        and `errors` take the retry's outcome."""
        failed = [leg for leg in dl.LEGS if status.get(leg) == "tracker_unavailable"]
        trace: dict[str, dict[str, Any]] = {
            leg: {"failure": dl.classify(errors[leg]) if leg in errors else None} for leg in failed
        }
        again = [leg for leg in failed if trace[leg]["failure"] and trace[leg]["failure"].transient]
        if not again:
            return trace, None
        delay = min(200, max(0, int(settings.tracker_dupcheck_leg_retry_delay_ms)))
        window = dl.retry_window(
            until,
            delay_ms=delay,
            reserve_ms=int(settings.tracker_dupcheck_leg_retry_reserve_ms),
            min_ms=int(settings.tracker_dupcheck_leg_retry_min_ms),
        )
        if window.skipped:
            logger.info(
                "tracker.duplicate_check on %s: %s not retried, %d ms left",
                self.sim_input.namespace,
                ",".join(again),
                window.left_ms,
            )
            for leg in again:
                trace[leg]["skipped"] = window.skipped
            return trace, None
        t0 = time.monotonic()
        if delay:
            await asyncio.sleep(delay / 1000.0)
        fresh: dict[str, str] = {}
        fresh_meta: dict[str, Any] = {}
        fresh_errors: dict[str, BaseException] = {}
        try:
            got, flight = await asyncio.wait_for(
                asyncio.gather(
                    self._legs(again, status, fresh, fresh_meta, fresh_errors),
                    self._flight(again, fresh, fresh_meta, fresh_errors),
                ),
                timeout=window.timeout_s,
            )
        except TimeoutError as e:
            got, flight = {}, []
            for leg in again:
                fresh[leg] = "tracker_unavailable"
                fresh_errors.setdefault(leg, e)
        ms = int((time.monotonic() - t0) * 1000)
        meta.setdefault("timings_ms", {})["legs_retry"] = ms
        meta.update({k: v for k, v in fresh_meta.items() if k != "timings_ms"})
        for leg in again:
            trace[leg].update(retried=True, retry_ms=ms)
            status[leg] = fresh.get(leg, "tracker_unavailable")
            if status[leg] == "tracker_unavailable":
                errors[leg] = fresh_errors.get(leg) or errors[leg]
                trace[leg]["failure"] = dl.classify(errors[leg])
                logger.info(
                    "tracker.duplicate_check/%s retry on %s: tracker_unavailable (%s)",
                    leg,
                    self.sim_input.namespace,
                    errors[leg],
                )
            else:
                errors.pop(leg, None)
                if got.get(leg):
                    lists[leg] = got[leg]
        return trace, (flight if "fix_path" in again else None)

    async def _legs(
        self,
        again: list[str],
        status: dict[str, str],
        fresh: dict[str, str],
        fresh_meta: dict[str, Any],
        fresh_errors: dict[str, BaseException],
    ) -> dict[str, list[dict[str, Any]]]:

        legs = {leg for leg in again if leg != "fix_path"}
        if not legs:
            return {}
        if "lexical" in legs and status.get("fields") == "served":
            legs.add("fields")
        return await tsim._similar_legs(
            self.src,
            self.sim_input,
            self.q,
            self.stats,
            self.item,
            ["issue"],
            legs,
            fresh,
            fresh_meta,
            before=self.wall,
            errors=fresh_errors,
        )

    async def _flight(
        self,
        again: list[str],
        fresh: dict[str, str],
        fresh_meta: dict[str, Any],
        fresh_errors: dict[str, BaseException],
    ) -> list[dict[str, Any]]:
        if "fix_path" not in again:
            return []
        inp = self.sim_input
        return await _fix_in_flight_leg(
            self.src,
            inp.org,
            inp.namespace,
            self.draft_paths,
            self.q,
            self.wall,
            fresh,
            fresh_meta,
            errors=fresh_errors,
        )


async def _fix_in_flight_leg(
    src: Any,
    org: str,
    ns: str,
    draft_paths: list[str],
    q: Any,
    wall: datetime | None,
    status: dict[str, str],
    meta: dict[str, Any],
    errors: dict[str, BaseException] | None = None,
) -> list[dict[str, Any]]:
    """The issues a fix in flight says it fixes. The draft's paths → the pull requests (open
    or merged, the last `tracker_dupcheck_fix_path_days`) whose diff touched them → the
    issues the tracker graph's `fixes` edges hang on those PRs → candidates, open PRs' first,
    each carrying the PR, its state and the paths it shares with the draft
    (`fix_paths`, `fix_pr_state`). A defect reported at symptom level and
    its root-cause report share no words and no frames, but the fix touches
    the file the finding names: that is the bridge. Silent
    (`fix_path: tracker_unavailable`) on an outage."""
    if not draft_paths:
        return []
    t0 = time.monotonic()
    try:
        prs = await src.prs_touching_paths(
            org,
            ns,
            draft_paths,
            before=wall,
            days=int(settings.tracker_dupcheck_fix_path_days),
            limit=int(settings.tracker_dupcheck_fix_path_prs),
        )
        by_ref = {str(p.get("source_id") or ""): p for p in prs if p.get("source_id")}
        edges = await src.edges_from(org, ns, sorted(by_ref), ["fixes"]) if by_ref else []
    except TrackerUnavailable as e:
        logger.info("tracker.duplicate_check/fix_in_flight on %s: tracker_unavailable (%s)", ns, e)
        status["fix_path"] = "tracker_unavailable"
        if errors is not None:
            errors["fix_path"] = e
        return []
    finally:
        meta.setdefault("timings_ms", {})["fix_in_flight"] = int((time.monotonic() - t0) * 1000)
    rows: dict[str, dict[str, Any]] = {}
    for edge in edges:
        pr = by_ref.get(str(edge.get("src_ref") or ""))
        ref = str(edge.get("dst_ref") or "")
        number = int(ref.rsplit("#", 1)[-1] or 0) if "#" in ref else 0
        if pr is None or number <= 0 or number == int(q.number or 0):
            continue
        if str(edge.get("dst_kind") or "") not in ("", "github_issue", "issue"):
            continue
        row = rows.setdefault(
            ref,
            {
                "kind": "issue",
                "number": number,
                "source_id": ref,
                "repo": ref.rsplit("#", 1)[0],
                "why": ["fix_path"],
                "fix_paths": {},
                "fix_pr_state": {},
            },
        )
        row["fix_paths"][str(pr["pr_number"])] = list(pr.get("hit_paths") or [])
        row["fix_pr_state"][str(pr["pr_number"])] = str(pr.get("state") or "open")
    order = {str(p["pr_number"]): i for i, p in enumerate(prs)}  # open first, newest first
    out = sorted(rows.values(), key=lambda r: min(order.get(pr, 1 << 20) for pr in r["fix_paths"]))
    status["fix_path"] = "served" if out else "empty"
    return out
