"""tracker.similar: the legs side by side (lexical, fields, frames,
mentions, vector), fused, hydrated and enriched with the record's relations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from server.core.errors import ValidationError, caller_text
from server.llm import customer
from server.settings import settings
from server.tracker.api.models import (
    TrackerSimilarInput,
    TrackerSimilarOutput,
)
from server.tracker.app import common
from server.tracker.app import source as tsource
from server.tracker.app.handlers import links_record
from server.tracker.domain.errors import TrackerUnavailable

logger = logging.getLogger(__name__)


_SIMILAR_LEGS = ("lexical", "fields", "frames", "mentions", "vector")


async def handle_tracker_similar(input: TrackerSimilarInput) -> TrackerSimilarOutput:
    """A search addressed to a repository reads every tracker linked to it. `issue_number`
    is the PRIMARY tracker's number (the repository's), so the other trackers are searched
    by that item's text, never by the same number — `#7` in a help desk is another item."""
    input = common._with_default_org(input)
    linked = await common.linked_trackers(str(input.org), str(input.namespace))
    if len(linked) <= 1:
        return await _similar_one(input)
    first = await _similar_one(input.model_copy(update={"namespace": linked[0]}))
    text = str(input.text or "")
    if int(input.issue_number or 0) > 0:
        try:
            item = await tsource.tracker_source(input.org, linked[0]).similar_item(
                input.org, linked[0], int(input.issue_number)
            )
        except TrackerUnavailable:
            item = None
        if item is not None:
            head = f"{item.get('title') or ''}\n{item.get('body') or ''}"
            text = head + (f"\n{text}" if text.strip() else "")
    rest = input.model_copy(update={"issue_number": 0, "text": text})
    others = await asyncio.gather(
        *[_similar_one(rest.model_copy(update={"namespace": ns})) for ns in linked[1:]]
        if text.strip() or rest.frames
        else [],
        return_exceptions=True,
    )
    return _merge_similar(linked, [first, *others], int(input.top_k))


def _merge_similar(namespaces: list[str], outs: list[Any], top_k: int) -> TrackerSimilarOutput:
    """One `tracker.similar` answer over several trackers: every item names
    its `namespace`; ranked by the fused score (reciprocal-rank based, so
    comparable across trackers); `query` and `legs` are the primary's; a
    tracker that failed inside the fan-out is left out and logged, except a
    held model call (`customer.HOLDS`), which is the answer."""
    served: list[tuple[str, Any]] = []
    for ns, o in zip(namespaces, outs, strict=False):
        if isinstance(o, customer.HOLDS):
            raise o  # the org's model calls are held: the request answers that
        if isinstance(o, BaseException):
            logger.info("tracker.similar on %s failed inside a fan-out: %s", ns, o)
            continue
        served.append((ns, o))
    items = [{**it, "namespace": ns} for ns, o in served for it in (o.items or [])]
    items.sort(key=lambda it: -float(it.get("score") or 0.0))
    first = served[0][1]
    return TrackerSimilarOutput(
        items=items[:top_k],
        query=dict(first.query or {}),
        legs=dict(first.legs or {}),
        namespaces=[ns for ns, _ in served],
        tracker_unavailable=all(bool(o.tracker_unavailable) for _, o in served),
    )


async def _similar_one(input: TrackerSimilarInput) -> TrackerSimilarOutput:
    input = await common._bound_input(input)
    if (
        not input.text.strip()
        and input.issue_number <= 0
        and not any(f.strip() for f in input.frames)
    ):
        # after the gate, so a refused principal never learns the shape
        raise ValidationError("text, issue_number or frames is required")
    from server.tracker.domain import similar as sim

    src = tsource.tracker_source(input.org, input.namespace)
    legs_on = set(input.legs) or set(_SIMILAR_LEGS)
    kinds = list(dict.fromkeys(input.kinds)) or ["issue", "pr"]
    try:
        q, stats, item = await _similar_query(src, input, sim)
    except TrackerUnavailable as e:
        common._unavailable("tracker.similar", input.namespace, e)
        return TrackerSimilarOutput(
            query={"number": int(input.issue_number), "known": False},
            legs={leg: "tracker_unavailable" for leg in _SIMILAR_LEGS if leg in legs_on},
            tracker_unavailable=True,
        )
    status: dict[str, str] = {}
    meta: dict[str, Any] = {}
    record: dict[str, Any] = {"status": "skipped", "relations": {}}
    ranked, record = await asyncio.gather(
        _similar_legs(src, input, q, stats, item, kinds, legs_on, status, meta),
        _similar_record(src, input, q, item),
    )
    # The vector leg adds its own candidates after the ranked ones and never re-ranks.
    fused = sim.fuse(
        ranked,
        agreement=True,
        agreement_weight=float(settings.tracker_similar_agreement_weight),
        agreement_max_rank=int(settings.tracker_similar_agreement_max_rank),
    )
    head = fused[: int(input.top_k)]
    t0 = time.monotonic()
    top = await _hydrate(src, input, head, status)
    if any(not r.get("title") for r in head):
        meta.setdefault("timings_ms", {})["hydrate"] = int((time.monotonic() - t0) * 1000)
    items = [_similar_item(r, record["relations"]) for r in top]
    ran = [v for k, v in status.items() if k != "fields" and v != "skipped"]
    return TrackerSimilarOutput(
        items=items,
        query={
            "number": q.number,
            "kind": q.kind,
            "known": item is not None,
            "terms": list(q.terms),
            "tokens": len(q.tokens),
            "frames": [f.raw for f in q.frames],
            "frames_source": q.frames_source,
            "mentions": list(q.mentions),
            "before": common._s(item.get("created_at")) if item else "",
            "component": q.component,
            "versions": list(q.versions),
            "error_code": q.error_code,
            "kinds": kinds,
            "record": record["status"],
            **meta,
        },
        legs=status,
        tracker_unavailable=bool(ran) and all(v == "tracker_unavailable" for v in ran),
    )


async def _similar_query(src: Any, input: Any, sim: Any) -> tuple[Any, Any, dict[str, Any] | None]:
    """The query: the item's own fields when a number is given (and its
    creation time as the wall), the pasted text otherwise; the corpus
    vocabulary; the terms worth a scan; the frames, from the caller or
    parsed out of the text."""
    item: dict[str, Any] | None = None
    number = int(input.issue_number or 0)
    if number > 0:
        item = await src.similar_item(input.org, input.namespace, number)
    raw = await src.token_stats(input.org, input.namespace)
    stats = sim.TokenStats(
        n=int(raw.get("n") or 0),
        df=dict(raw.get("df") or {}),
        avg_title_len=float(raw.get("avg_title_len") or 10.0),
        avg_body_len=float(raw.get("avg_body_len") or 540.0),
    )
    text = str(input.text or "")
    title = text.strip().splitlines()[0][:200] if text.strip() else ""
    if item is not None:
        title = str(item.get("title") or "")
        text = f"{title}\n{item.get('body') or ''}" + (f"\n{text}" if text.strip() else "")
    tokens = sim.query_tokens(text)
    q = sim.Query(
        number=number,
        kind=str(item.get("kind") or "") if item else "",
        text=text,
        title=title,
        component=str(item.get("component") or "") if item else "",
        versions=list(item.get("versions") or []) if item else [],
        error_code=int(item.get("error_code") or 0) if item else 0,
        tokens=tokens,
        terms=sim.select_terms(
            tokens, stats, prefer=sim.query_tokens(title), defer=sim.camel_pieces(text)
        ),
    )
    if not q.error_code:
        q.error_code = sim.error_code_of(text)
    caller_frames = [f for f in (input.frames or []) if str(f).strip()]
    if caller_frames:
        q.frames, q.frames_source = sim.normalise_frames(caller_frames), "caller"
    elif text:
        q.frames = sim.frames_from_text(text)
        q.frames_source = "body" if q.frames else ""
    q.frames = sim.select_frames(q.frames, stats)
    q.mentions = [n for n in sim.mentioned_numbers(text) if n != number]
    return q, stats, item


async def _similar_legs(
    src: Any,
    input: Any,
    q: Any,
    stats: Any,
    item: dict[str, Any] | None,
    kinds: list[str],
    legs_on: set[str],
    status: dict[str, str],
    meta: dict[str, Any] | None = None,
    before: Any = None,
    errors: dict[str, BaseException] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Run the legs side by side; each records its status and returns its
    ranked list (empty when it did not run); `meta` collects what the
    query block reports beside the legs (the vector leg's catch-up);
    `errors` receives a failed leg's exception.
    `before` overrides the wall (a draft replayed at its filing time); by default it is the
    query item's creation time."""
    from server.tracker.domain import similar as sim

    before = before or (item.get("created_at") if item else None)
    exclude = str(item.get("source_id") or "") if item else ""
    fields = "fields" in legs_on and "lexical" in legs_on
    status["fields"] = "served" if fields else "skipped"

    async def lexical() -> list[dict[str, Any]]:
        if "lexical" not in legs_on or not q.terms or not any(k in ("issue", "pr") for k in kinds):
            status["lexical"] = "skipped"
            return []
        idf = [stats.idf(t) for t in q.terms]
        rows: list[dict[str, Any]] = []
        for kind in (k for k in kinds if k in ("issue", "pr")):
            rows.extend(
                await src.similar_lexical(
                    input.org,
                    input.namespace,
                    kind=kind,
                    terms=q.terms,
                    idf=idf,
                    avg_title_len=stats.avg_title_len,
                    avg_body_len=stats.avg_body_len,
                    before=before,
                    exclude=exclude,
                    limit=50,
                    title_weight=sim.TITLE_WEIGHT,
                )
            )
        status["lexical"] = "served" if rows else "empty"
        return sim.rank_lexical(rows, q, fields=fields)

    async def frames() -> list[dict[str, Any]]:
        if "frames" not in legs_on or not q.frames:
            status["frames"] = "skipped"
            return []
        return await _similar_frames_leg(src, input, q, stats, kinds, before, exclude, status)

    async def mentions() -> list[dict[str, Any]]:
        if (
            "mentions" not in legs_on
            or not q.mentions
            or not any(k in ("issue", "pr") for k in kinds)
        ):
            status["mentions"] = "skipped"
            return []
        rows = await src.similar_mentions(
            input.org,
            input.namespace,
            numbers=q.mentions,
            kinds=[k for k in kinds if k in ("issue", "pr")],
            before=before,
            exclude=exclude,
        )
        status["mentions"] = "served" if rows else "empty"
        return [{**r, "why": ["mentions"]} for r in rows]

    async def vector() -> list[dict[str, Any]]:
        # In the default set only when the flag says so; a caller naming the leg runs it.
        armed = "vector" in set(input.legs) or bool(settings.tracker_similar_vector_leg)
        if "vector" not in legs_on or not armed or not any(k in ("issue", "pr") for k in kinds):
            status["vector"] = "skipped"
            return []
        try:
            return await _similar_vector_leg(
                src, input, q, item, kinds, before, status, meta if meta is not None else {}
            )
        except TrackerUnavailable:
            raise
        except customer.HOLDS:
            raise  # the org's model calls are held: the request answers that, not an outage
        except Exception as e:
            # The leg's own store or embedder failed: the leg is unavailable and the
            # answer degraded (`_guarded` records it), never a 500 that also leaves the
            # sibling legs running.
            logger.warning("tracker.similar vector leg failed: %s: %s", type(e).__name__, e)
            raise TrackerUnavailable(f"vector leg: {caller_text(e)}") from e

    timings: dict[str, int] = {}
    if meta is not None:
        meta["timings_ms"] = timings

    def leg(name: str, coro: Any) -> Any:
        return _guarded(
            name, coro, ns=input.namespace, status=status, errors=errors, timings=timings
        )

    # Every leg runs to its end; then a held model call (`customer.HOLDS`) is the
    # request's answer, rather than a degraded one without the vector leg.
    results = await asyncio.gather(
        leg("lexical", lexical()),
        leg("frames", frames()),
        leg("mentions", mentions()),
        leg("vector", vector()),
        return_exceptions=True,
    )
    legs: dict[str, list[dict[str, Any]]] = {}
    for res in results:
        if isinstance(res, BaseException):
            raise res
        if res[1]:
            legs[res[0]] = res[1]
    return legs


async def _guarded(
    name: str,
    coro: Any,
    ns: str,
    status: dict[str, str],
    errors: dict[str, BaseException] | None,
    timings: dict[str, int],
) -> tuple[str, list[dict[str, Any]]]:
    """One leg's rows; an unavailable leg is named in `status` (and `errors`) and
    answers none, so its siblings still serve."""
    t0 = time.monotonic()
    try:
        return name, await coro
    except TrackerUnavailable as e:
        common._unavailable(f"tracker.similar/{name}", ns, e)
        status[name] = "tracker_unavailable"
        if errors is not None:
            errors[name] = e
        return name, []
    finally:
        timings[name] = int((time.monotonic() - t0) * 1000)


async def _similar_frames_leg(
    src: Any,
    input: Any,
    q: Any,
    stats: Any,
    kinds: list[str],
    before: Any,
    exclude: str,
    status: dict[str, str],
) -> list[dict[str, Any]]:
    """Position-decayed frame IDF over the candidates that carry any of the
    query's frames: issues by body, incidents (restricted namespaces whose source provides
    `core_incidents`) by `stack_functions` suffix and body."""
    from server.tracker.app import contract
    from server.tracker.domain import similar as sim

    frame_tokens = [f.token for f in q.frames]
    paths = [f.path for f in q.frames if f.kind == "file" and f.path]
    path_index = [i for i, f in enumerate(q.frames) if f.kind == "file" and f.path]
    scored: list[dict[str, Any]] = []
    cfg = getattr(src, "cfg", None)
    for kind in (k for k in kinds if k in ("issue", "incident")):
        if kind == "incident" and not getattr(cfg, "restricted", False):
            status["frames_incident"] = "restricted_required"
            continue
        if kind == "incident" and not contract.provides(cfg, "core_incidents"):
            status["frames_incident"] = "not_provided"
            continue
        rows = await src.similar_frames(
            input.org,
            input.namespace,
            kind=kind,
            frame_tokens=frame_tokens,
            paths=paths,
            before=before,
            exclude=exclude,
        )
        for r in rows:
            hits = list(r.get("frame_hits") or [])
            hits += [False] * (len(q.frames) - len(hits))
            for j, hit in enumerate(r.get("path_hits") or []):
                if hit and j < len(path_index):
                    hits[path_index[j]] = True
            score = sim.frame_score(hits, q.frames, stats)
            if score > 0:
                scored.append({**r, "leg_score": score, "why": ["frames"]})
    scored.sort(key=lambda r: (-r["leg_score"], str(r.get("source_id") or "")))
    status["frames"] = "served" if scored else "empty"
    return scored[:50]


async def _similar_vector_leg(
    src: Any,
    input: Any,
    q: Any,
    item: dict[str, Any] | None,
    kinds: list[str],
    before: Any,
    status: dict[str, str],
    meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """The nearest items by cosine over the vectors loom computed itself
    (`loom.qa_item_embeddings`, the `tracker_embed` pass) for the addressed namespace — or,
    when the namespace's upstream `embedding` column is populated and the item carries one,
    QA's own column through the upstream-embedding reader (the pass is a no-op then). Two
    things run side by side: the scan (the query vector — the item's stored row, a point
    read, else the text embedded once through loom's embedder and the shared cache — then
    the cosine scan under the wall) and the query-time catch-up (`embed_index.catch_up`: the
    items changed upstream since the pass's cursor, embedded in one batched call, written
    so the next call pays nothing); the fresh candidates are ranked in process and merged.
    The rows carry keys and a distance only — the table holds no text — and
    the ones that reach the answer are hydrated after fusion (`_hydrate`).
    `served` with rows; `empty` when the namespace has rows but none
    qualified; `not_computed` when it has none (the pass has not run)."""
    from server.tracker.app import embed_index as ei

    repo = _embed_repo()
    org, ns = input.org, input.namespace
    if (
        item is not None
        and item.get("has_embedding")
        and await src.upstream_embeddings_populated(org, ns)
    ):
        # QA's own column is populated: read it through the upstream-embedding reader
        rows = await src.similar_vector(org, ns, number=q.number, before=before, limit=50)
        status["vector"] = "served" if rows else "empty"
        return [{**r, "why": ["vector"]} for r in rows]
    exclude = (str(item.get("kind") or "issue"), q.number) if item is not None else None
    wanted = [k for k in kinds if k in ("issue", "pr")]

    timings: dict[str, int] = meta.setdefault("timings_ms", {})

    async def scan() -> tuple[list[float] | None, list[dict[str, Any]]]:
        t0 = time.monotonic()
        vector, dim = await _vector_query(repo, input, q, item)
        timings["vector_query"] = int((time.monotonic() - t0) * 1000)
        if vector is None:
            return None, []
        t1 = time.monotonic()
        rows = await src.similar_vector_local(
            org,
            ns,
            vector=vector,
            dim=dim,
            kinds=wanted,
            before=before,
            exclude=exclude,
            limit=50,
            repo=repo,
        )
        timings["vector_scan"] = int((time.monotonic() - t1) * 1000)
        return vector, rows

    async def fresh_leg() -> Any:
        t0 = time.monotonic()
        try:
            return await _vector_catch_up(src, repo, org, ns, kinds=wanted)
        finally:
            timings["vector_catch_up"] = int((time.monotonic() - t0) * 1000)

    started = time.monotonic()
    catching_up = asyncio.ensure_future(fresh_leg())
    _BACKGROUND.add(catching_up)
    catching_up.add_done_callback(_BACKGROUND.discard)
    vector, rows = await scan()
    share = int(settings.tracker_similar_catch_up_budget_ms) / 1000.0
    budget = max(0.0, share - (time.monotonic() - started))
    try:
        fresh = await asyncio.wait_for(asyncio.shield(catching_up), timeout=budget)
    except TimeoutError:
        # past its share of the budget: the table answers now; the embedding
        # finishes in the background and the next call finds the rows stored
        fresh = ei.CatchUp(status="pending")
        meta["fresh_pending"] = True
    meta["fresh_items"] = fresh.embedded
    meta["fresh_left_to_pass"] = fresh.left_to_pass
    if vector is None:
        status["vector"] = "skipped"  # nothing to embed: no text, no stored row
        return []
    rows = ei.merge_fresh(
        rows, ei.rank_fresh(fresh.hits, vector, before=before, exclude=exclude), limit=50
    )
    if rows:
        status["vector"] = "served"
        return [{**r, "why": ["vector"]} for r in rows]
    stats = await repo.namespace_stats(org, ns)
    status["vector"] = "empty" if stats.get("live") else "not_computed"
    return []


async def _vector_query(
    repo: Any,
    input: Any,
    q: Any,
    item: dict[str, Any] | None,
) -> tuple[list[float] | None, int]:
    """The query vector: the item's own stored row when the table has one
    (a point read, no embedding call), else the query text embedded once
    over the recipe the pass writes (`embed_text`) through loom's embedder
    and the shared query-embedding cache; (None, 0) when there is no text."""
    from server.embedding.embedder import get_embedder
    from server.tracker.app.embed_index import embed_text

    if item is not None and q.number > 0:
        stored = await repo.stored_vector(
            input.org, input.namespace, str(item.get("kind") or "issue"), q.number
        )
        if stored is not None:
            return stored["embedding"], int(stored["embedding_dim"])
    body = str(item.get("body") or "") if item is not None else str(input.text or "")
    title = q.title if item is not None else ""
    text = embed_text(title, body, body_chars=int(settings.tracker_embed_body_chars))
    if not text.strip():
        return None, 0
    vector = await get_embedder().embed(text)
    return vector, len(vector)


async def _vector_catch_up(src: Any, repo: Any, org: str, ns: str, *, kinds: list[str]) -> Any:
    """The items changed upstream since the pass's per-kind cursor, embedded and written
    now. A kind the pass never ran for has no cursor and is left alone; a failure here
    never fails the leg."""
    from server.embedding.embedder import get_embedder
    from server.tracker.app import embed_index as ei

    try:
        state = ei.cursor_state()
        got = await asyncio.gather(*(state.get(org, ns, k) for k in kinds))
        cursors = dict(zip(kinds, got, strict=True))
        if not any(cursors.values()):
            return ei.CatchUp()
        return await ei.catch_up(
            src,
            repo,
            get_embedder(),
            org,
            ns,
            kinds=kinds,
            cursors=cursors,
            closed_days=ei.window_days_by_kind(),
            body_chars=int(settings.tracker_embed_body_chars),
        )
    except Exception as e:  # noqa: BLE001 — the table's answer stands
        logger.info("tracker.similar/vector catch-up on %s failed: %s: %s", ns, type(e).__name__, e)
        return ei.CatchUp(status="tracker_unavailable")


# The legs whose rows can reach the answer without a title (a `why` also carries
# field-equality reasons, which are not legs).
_HYDRATED_LEGS = frozenset({"lexical", "frames", "mentions", "vector", "fix_path"})


async def _hydrate(
    src: Any, input: Any, rows: list[dict[str, Any]], status: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """A vector hit carries keys and a distance only (the table is a derived index, no
    text); the hits that reach the answer are read by key from the addressed namespace's
    source, live — titles and state are the upstream's, exactly as the lexical leg's are,
    and a hit the upstream no longer serves drops out. A row another leg already hydrated
    is left as it is; a failed read drops the un-hydrated rows, and every leg that
    found one of them is marked `tracker_unavailable` in `status`: its candidates never
    reached the answer, so a `none` without them is not clearance."""
    need = [r for r in rows if not r.get("title") and int(r.get("number") or 0) > 0]
    if not need:
        return rows
    try:
        got = await src.similar_mentions(
            input.org,
            input.namespace,
            numbers=[int(r["number"]) for r in need],
            kinds=sorted({str(r.get("kind") or "issue") for r in need}),
            before=None,
            max_numbers=len(need),
        )
    except TrackerUnavailable as e:
        common._unavailable("tracker.similar/hydrate", input.namespace, e)
        got = []
        if status is not None:
            for leg in {str(w) for r in need for w in r.get("why") or []} & _HYDRATED_LEGS:
                status[leg] = "tracker_unavailable"
    by_key = {(str(g.get("kind") or ""), int(g.get("number") or 0)): g for g in got}
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("title") or int(r.get("number") or 0) <= 0:
            out.append(r)
            continue
        g = by_key.get((str(r.get("kind") or ""), int(r["number"])))
        if g is not None:
            # the upstream's fields win, except what the leg computed for this
            # row (its score, why, distance and the fix PRs that led to it) and any
            # annotation the upstream lacks
            keep = {k: v for k, v in r.items() if k not in g or k in ("score", "why", "distance")}
            out.append({**g, **keep})
    return out


def _embed_repo() -> Any:
    from server.tracker.store.embed import TrackerEmbedRepo

    return TrackerEmbedRepo()


# The catch-ups still embedding after their call answered: held here so the loop never
# drops a running task; awaited by tests.
_BACKGROUND: set[asyncio.Task[Any]] = set()


async def _similar_record(
    src: Any,
    input: Any,
    q: Any,
    item: dict[str, Any] | None,
) -> dict[str, Any]:
    """The relation the tracker graph already records between the query item and
    a candidate — `duplicate_of` | `related_to` | `fixed_by` — so a caller
    can tell "this IS the same issue" from "this is the same pattern"; the leg's job stays
    recall, the verdict is the caller's. Reuses the `duplicates_of` and `fixes` readers;
    only an `issue_number` query has an anchor to read from."""
    out: dict[str, Any] = {"status": "skipped", "relations": {}}
    if item is None or q.number <= 0 or str(item.get("kind") or "issue") != "issue":
        return out
    org, ns = input.org, input.namespace
    try:
        edges, fixes = await asyncio.gather(
            src.duplicates_of(org, ns, number=q.number, limit=50),
            links_record(src, org, ns, issue=q.number, pr=0),
        )
    except TrackerUnavailable as e:
        common._unavailable("tracker.similar/record", ns, e)
        return {"status": "tracker_unavailable", "relations": {}}
    rel: dict[str, str] = {}
    for edge in edges:
        sid, kind = str(edge.get("source_id") or ""), str(edge.get("rel_type") or "")
        if sid and kind in ("duplicate_of", "related_to") and rel.get(sid) != "duplicate_of":
            rel[sid] = kind
    from server.tracker.domain.namespaces import ref_of

    for repo_name in src.repos():
        for n in fixes.get("numbers") or []:
            rel.setdefault(ref_of(repo_name, int(n)), "fixed_by")
    if fixes.get("status") == "tracker_unavailable":
        out["status"] = "tracker_unavailable"
    else:
        out["status"] = "served" if rel else "empty"
    out["relations"] = rel
    return out


def _similar_item(r: dict[str, Any], relations: dict[str, str] | None = None) -> dict[str, Any]:
    relation = (relations or {}).get(str(r.get("source_id") or ""), "")
    why = list(r.get("why") or [])
    if relation and relation not in why:
        why.append(relation)
    return {
        "kind": str(r.get("kind") or ""),
        "number": int(r.get("number") or 0),
        "repo": str(r.get("repo") or ""),
        "title": str(r.get("title") or ""),
        "state": str(r.get("state") or ""),
        "state_reason": str(r.get("state_reason") or ""),
        "component": str(r.get("primary_component") or ""),
        "score": round(float(r.get("score") or 0.0), 5),
        # the vector leg's cosine distance when it saw the item
        "distance": round(float(r["distance"]), 4) if r.get("distance") is not None else None,
        "why": why,
        "relation": relation or "none",
        "url": str(r.get("url") or ""),
        "created_at": common._s(r.get("created_at")),
    }


# ---- tracker.links ------------------------------------------------------------------
