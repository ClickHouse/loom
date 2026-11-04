"""namespace.tune's document drafts: the chunk size and stride, and an extractor route per
format, from what the namespace's documents show.

Both are deployment settings, the only place either lives: `knowledge_chunk_size` and
`knowledge_chunk_stride` (live, changed by a platform admin through settings.set) cut
every namespace's next writes, and LOOM_DOCUMENT_EXTRACTORS_JSON (read at startup) routes
every namespace's next ingests. A draft names the setting, its current and proposed
value, the evidence from this namespace and the write that would apply it. Nothing is
applied and no model is called.

The chunking evidence is a bounded sample of the live documents cut again in process at
the current values and at each candidate, by the chunker its write path uses: a text
document's text through `chunk_document`, an ingested one's stored artifact through
`chunk_artifact`. Nothing is written and no text leaves this module. A draft is made by
one of two rules (version 1), judged on the sample:

- over the limit: a chunk's embedding input is over the embedder's input limit, which
  that provider refuses: the largest smaller size that brings every input under it;
- sections cut: the size cuts more than a quarter of the sampled sections into more
  chunks than their structure alone makes, over at least `MIN_SAMPLE_DOCUMENTS`
  documents, and the embedder's limit is known: the smallest larger size, up to four
  times the current, that keeps three quarters whole with every input under the limit.

The stride keeps its ratio to the size. Otherwise nothing is drafted, and the sample
stands as the evidence.

The extractor evidence is the namespace's knowledge.ingest jobs whose current job failed
`format_unavailable` and the published extractions that are incomplete, per format, and
what each configured service declares (`GET /v1/capabilities`). A format is drafted
only when a configured service can answer it (`_judge_format` states the one ranking);
otherwise `skipped` says that none does. A service's URL can carry a credential: it is
named by the setting that holds it, never shown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Any

from server.core.document_formats import format_of, format_of_media_type
from server.embedding import count_tokens, get_embedder, input_token_limit
from server.identity.api.models_namespace_tune import (
    ChunkingPreview,
    ChunkingSample,
    ChunkStats,
    NamespaceTuneInput,
    TuneApply,
    TuneEvidence,
    TuneProposal,
)
from server.knowledge.app import extractors
from server.knowledge.app.ingest_run import JOB_LEASE_S
from server.knowledge.domain.chunking import chunk_context, chunk_document, embedding_text
from server.knowledge.domain.extract import Artifact, parse_artifact
from server.knowledge.domain.extract.structural import chunk_artifact
from server.knowledge.store import blobs
from server.knowledge.store import inspect as inspect_store
from server.knowledge.store import tune as store
from server.settings import settings

log = logging.getLogger(__name__)

# Guarded(section, read, default): one section's read, a failure in `skipped`.
Guarded = Callable[[str, Callable[[], Awaitable[Any]], Any], Awaitable[Any]]

# A sampled text document longer than this is not read; an ingested document whose stored
# artifact is larger is not either. Both count as `unread`.
MAX_DOCUMENT_CHARS = 2_000_000  # not a setting: bounds one request's memory
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024  # not a setting: bounds one request's memory
# The most the whole sample loads: characters of text plus bytes of artifact. It bounds
# one call's memory and the CPU of its cuts (each loaded character is cut and counted up
# to six times); a document past it counts as `unread`.
MAX_SAMPLE_CHARS = 8 * 1024 * 1024  # not a setting: bounds one viewer call's memory and CPU
# The sections-cut rule: a size cutting more than this share of the sampled sections
# drafts a larger one, which must cut at most this share. A change is a new rule version.
CUT_SHARE = 0.25  # not a setting: the rule's threshold, versioned with it
# Fewer sampled documents than this judge no section rule.
MIN_SAMPLE_DOCUMENTS = 5  # not a setting: the rule's threshold, versioned with it
# The candidates tried, as factors of the current size, nearest first.
_SMALLER = (3 / 4, 1 / 2, 1 / 3, 1 / 4)
_LARGER = (3 / 2, 2, 3, 4)
MIN_CHUNK_SIZE = 200  # not a setting: the smallest size drafted
# A size large enough that nothing is windowed: what a section's structure alone makes.
_WHOLE = 1 << 62
# Seconds the capabilities read of every configured extractor, run at once, may take
# here; a slower one reads as unread.
CAPABILITIES_READ_S = 5.0  # not a setting: a draft's read, which must not hold the report
# Agents named as affected, most documents first.
MAX_AFFECTED_AGENTS = 20  # not a setting: bounds one response

_SCOPE = "every namespace of this deployment (a deployment setting)"
_HISTORICAL_CHUNKS = (
    "Documents already stored keep their chunks. A text document is cut again by loom "
    "rechunk --org <org> --namespace <namespace> --apply; an ingested one only by a new "
    "knowledge.ingest of its original (loom rechunk would turn it into a text document). "
    "Each is a separate action with its own approval."
)
_HISTORICAL_ROUTES = (
    "Sources already ingested keep their extraction, and a failed job does not run again: "
    "each is re-extracted only by uploading its original again and running knowledge.ingest, "
    "a separate action with its own approval."
)
DECISIONS_SKIPPED = (
    "decision_extraction: no instruction is drafted: loom decisions extract and the source "
    "sync's mining build their prompt from fixed text, and no request, namespace or org "
    "setting carries instructions for them"
)


@dataclass
class DocumentDrafts:
    # Each proposal's `id` is left empty for the handler to set.
    proposals: list[TuneProposal] = field(default_factory=list)
    chunking: ChunkingSample | None = None


@dataclass
class _Doc:
    uri: str
    title: str
    description: str
    text: str = ""
    artifact: Artifact | None = None
    # Chunks per section at a size that windows nothing; set on the first cut.
    whole: Counter[Hashable] | None = None


def _cut(doc: _Doc, size: int, stride: int) -> list[tuple[Hashable, str, str]]:
    """(section, text, breadcrumb) per chunk, as the document's write path cuts it."""
    if doc.artifact is not None:
        return [
            (c.section_path, c.text, c.breadcrumb)
            for c in chunk_artifact(doc.artifact, size=size, stride=stride)
        ]
    return [
        (c.section_idx, c.text, c.breadcrumb)
        for c in chunk_document(doc.text, size=size, stride=stride)
    ]


@dataclass
class _Measure:
    stats: ChunkStats
    # The documents with a section cut, and with an input over the limit.
    cut: list[str]
    over: list[str]


def _measure(docs: list[_Doc], size: int, stride: int, limit: int | None) -> _Measure:
    """The sample cut at `size` and `stride`, a stride the caller has clamped to the size."""
    lengths: list[int] = []
    sections = cut_sections = over = max_tokens = 0
    cut_docs: list[str] = []
    over_docs: list[str] = []
    for d in docs:
        if d.whole is None:
            d.whole = Counter(s for s, _, _ in _cut(d, _WHOLE, _WHOLE))
        chunks = _cut(d, size, stride)
        per = Counter(s for s, _, _ in chunks)
        cut_here = sum(1 for s, n in per.items() if n > d.whole.get(s, 0))
        sections += len(d.whole)
        cut_sections += cut_here
        if cut_here:
            cut_docs.append(d.uri)
        over_here = 0
        for _, text, crumb in chunks:
            lengths.append(len(text))
            tokens = count_tokens(
                embedding_text(chunk_context(d.title, d.description, crumb), text)
            )
            max_tokens = max(max_tokens, tokens)
            if limit is not None and tokens > limit:
                over_here += 1
        over += over_here
        if over_here:
            over_docs.append(d.uri)
    lengths.sort()
    n = len(lengths)
    stats = ChunkStats(
        size=size,
        stride=stride,
        chunks=n,
        min_chars=lengths[0] if n else 0,
        median_chars=lengths[(n - 1) // 2] if n else 0,
        p90_chars=lengths[max(0, math.ceil(0.9 * n) - 1)] if n else 0,
        max_chars=lengths[-1] if n else 0,
        sections=sections,
        sections_cut=cut_sections,
        max_input_tokens=max_tokens,
        over_input_limit=None if limit is None else over,
    )
    return _Measure(stats, cut_docs, over_docs)


def _scaled(size: int, factor: float) -> int:
    return max(MIN_CHUNK_SIZE, int(size * factor) // 50 * 50)


def _stride_for(new_size: int, size: int, stride: int) -> int:
    """The stride at `new_size` with the current `stride`'s ratio to the positive `size`."""
    return max(1, min(new_size, round(stride * new_size / size)))


async def _load(org: str, namespace: str, rows: list[dict[str, Any]]) -> tuple[list[_Doc], int]:
    """The sampled documents with what each is cut from, in sample order until
    MAX_SAMPLE_CHARS is loaded, and how many were not read: too long, past the total, or
    an artifact that could not be read."""
    docs: list[_Doc] = []
    unread = loaded = 0
    for r in rows:
        doc = _Doc(str(r["source_uri"]), str(r["title"]), str(r["description"]))
        sha = str(r["artifact_sha256"])
        if not sha:
            chars, doc.text = int(r["chars"]), str(r["text"])
            # The read leaves out a text past its own bound or the total ('' for a
            # non-empty document); this total also counts the artifacts loaded.
            if (chars and not doc.text) or loaded + chars > MAX_SAMPLE_CHARS:
                unread += 1
                continue
            loaded += chars
            docs.append(doc)
            continue
        try:
            stored = await blobs.locate(org, namespace, sha)
            if (
                stored is None
                or stored.size > MAX_ARTIFACT_BYTES
                or loaded + stored.size > MAX_SAMPLE_CHARS
            ):
                unread += 1
                continue
            loaded += stored.size
            data = await blobs.read(org, namespace, sha, stored=stored)
            if data is None:
                unread += 1
                continue
            doc.artifact = await asyncio.to_thread(
                parse_artifact,
                data,
                fmt=str(r["doc_format"]),
                max_bytes=settings.document_extractor_max_artifact_bytes,
            )
        except Exception as e:  # noqa: BLE001 — one unreadable artifact must not sink the sample
            log.warning("namespace.tune: artifact of %s not read: %s", doc.uri, type(e).__name__)
            unread += 1
            continue
        docs.append(doc)
    return docs, unread


def _setting_apply(env: str, value: int, current: int) -> TuneApply:
    """settings.set of the setting `env` names (LOOM_<FIELD>, the name a person sees in
    the configuration reference), guarded by its current value."""
    return TuneApply(
        op="settings.set",
        via="admin_route",
        args={"name": env.removeprefix("LOOM_").lower(), "value": value, "expected": current},
        required_role="platform_admin",
        guard=(
            "expected: the change is refused when the value in force is no longer the "
            "drafted current one."
        ),
    )


async def _chunking(
    inp: NamespaceTuneInput,
    guarded: Guarded,
    skipped: list[str],
    population: int | None,
    agents: list[str],
    total: int | None,
) -> tuple[ChunkingSample | None, TuneProposal | None]:
    org, ns = inp.org, inp.namespace
    cap = settings.namespace_tune_document_sample
    rows = await guarded(
        "chunking",
        lambda: store.sample_documents(
            org, ns, limit=cap, max_chars=MAX_DOCUMENT_CHARS, total_chars=MAX_SAMPLE_CHARS
        ),
        None,
    )
    if not rows:
        return None, None
    embedder = await guarded("chunking.embedder", _embedder, None)
    if embedder is None:
        return None, None
    model, limit = embedder
    docs, unread = await _load(org, ns, rows)
    if not docs:
        skipped.append(f"chunking: none of the {len(rows)} sampled documents could be read")
        return None, None
    # Read once: the values in force for the whole draft, and the guard it carries.
    size, set_stride = settings.knowledge_chunk_size, settings.knowledge_chunk_stride
    stride = min(set_stride, size)
    before = await asyncio.to_thread(_measure, docs, size, stride, limit)
    sample = ChunkingSample(
        cap=cap,
        population=population,
        documents=len(docs),
        unread=unread,
        load_cap=MAX_SAMPLE_CHARS,
        embedder_model=model,
        input_token_limit=limit,
        current=before.stats,
    )
    cur = before.stats
    if size <= 0:
        skipped.append(
            f"chunking: knowledge_chunk_size is {size} (no window: a text document is one "
            "chunk, an ingested one a chunk per block); no size is drafted"
        )
        return sample, None
    if set_stride <= 0:
        skipped.append(
            f"chunking: knowledge_chunk_stride is {set_stride} (no window: a section or block "
            "longer than the size is one chunk); no size is drafted"
        )
        return sample, None
    if limit is not None and cur.over_input_limit:
        flagged, shrink = before.over, True
        # Each smaller size once, nearest first.
        sizes = sorted(
            {_scaled(size, f) for f in _SMALLER if _scaled(size, f) < size}, reverse=True
        )
        why = (
            f"{cur.over_input_limit} of {cur.chunks} sampled chunks have an embedding input "
            f"over {model}'s {limit}-token limit (the largest is {cur.max_input_tokens}), "
            "which the provider refuses, so a write cut like them fails"
        )
        fits = "every input under the limit"
    elif cur.sections and cur.sections_cut > CUT_SHARE * cur.sections:
        share = f"{cur.sections_cut} of {cur.sections} sampled sections"
        if len(docs) < MIN_SAMPLE_DOCUMENTS:
            skipped.append(
                f"chunking: the size cuts {share}, judged on {len(docs)} documents, fewer "
                f"than the {MIN_SAMPLE_DOCUMENTS} the rule needs"
            )
            return sample, None
        if limit is None:
            skipped.append(
                f"chunking: the size cuts {share}, and no larger size is drafted: "
                f"{model}'s input limit is not known, so a longer chunk could be cut short "
                "or refused when embedded"
            )
            return sample, None
        flagged, shrink = before.cut, False
        # Each larger size once, nearest first.
        sizes = sorted({_scaled(size, f) for f in _LARGER} - {size})
        why = (
            f"the size cuts {share} into more chunks than their structure makes, so a "
            "search hit returns part of a section"
        )
        fits = (
            f"at most {CUT_SHARE:.0%} of the sections cut and every input under the "
            "embedder's limit"
        )
    else:
        return sample, None
    if not sizes:
        skipped.append(
            f"chunking: {why}; knowledge_chunk_size {size} is at or below the smallest "
            f"drafted size, {MIN_CHUNK_SIZE}"
        )
        return sample, None
    after = None
    for new in sizes:
        m = await asyncio.to_thread(_measure, docs, new, _stride_for(new, size, stride), limit)
        if m.stats.over_input_limit == 0 and (
            shrink or m.stats.sections_cut <= CUT_SHARE * m.stats.sections
        ):
            after = m.stats
            break
    if after is None:
        tried = ", ".join(str(n) for n in sizes)
        skipped.append(f"chunking: {why}; no size tried ({tried}) gives {fits}")
        return sample, None
    return sample, _chunking_proposal(
        inp, sample, before, after, why, flagged, agents, total, size, set_stride
    )


def _chunking_proposal(
    inp: NamespaceTuneInput,
    sample: ChunkingSample,
    before: _Measure,
    after: ChunkStats,
    why: str,
    flagged: list[str],
    agents: list[str],
    total: int | None,
    size: int,
    set_stride: int,
) -> TuneProposal:
    cur = before.stats
    also = (
        [_setting_apply("LOOM_KNOWLEDGE_CHUNK_STRIDE", after.stride, set_stride)]
        if after.stride != set_stride
        else []
    )
    return TuneProposal(
        id="",
        target="settings.knowledge_chunking",
        scope=_SCOPE,
        title=f"Cut documents into chunks of {after.size} characters",
        rationale=(
            f"Of {sample.documents} documents of {inp.org}/{inp.namespace} cut again at the "
            f"current values, {why}. At size {after.size} and stride {after.stride} the same "
            f"sample makes {after.chunks} chunks (now {cur.chunks}), cuts "
            f"{after.sections_cut} of {after.sections} sections (now {cur.sections_cut}) and "
            f"its largest embedding input is {after.max_input_tokens} tokens (now "
            f"{cur.max_input_tokens}). The stride keeps its ratio to the size."
        ),
        current=f"LOOM_KNOWLEDGE_CHUNK_SIZE={size}, LOOM_KNOWLEDGE_CHUNK_STRIDE={set_stride}",
        proposed=(
            f"LOOM_KNOWLEDGE_CHUNK_SIZE={after.size}, LOOM_KNOWLEDGE_CHUNK_STRIDE={after.stride}"
        ),
        evidence=TuneEvidence(
            supporting_count=len(flagged),
            documents=flagged[: inp.sample_limit],
            counter_count=sample.documents - len(flagged),
        ),
        affected_agents=agents,
        affected_agents_total=total,
        historical=_HISTORICAL_CHUNKS,
        apply=_setting_apply("LOOM_KNOWLEDGE_CHUNK_SIZE", after.size, size),
        apply_also=also,
        preview=ChunkingPreview(before=cur, after=after),
        limits=(
            "The settings are the deployment's: every namespace's next writes are cut at "
            "them, and the evidence is this namespace's sample. A longer chunk keeps more of "
            "a section in one vector and a shorter one less; what either does to search "
            "quality is not measured here, which a paired search benchmark shows. Until "
            "both settings are set, a write uses a stride of at most the size."
        ),
    )


async def _embedder() -> tuple[str, int | None]:
    e = get_embedder()
    return str(e.model_name), input_token_limit(e)


def _failing_formats(rows: list[dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    """format -> the URL of the service its sources go to -> the source_uris whose current
    job failed format_unavailable: the format and the service read as knowledge.ingest
    read them, from the job's name, media type and declared format."""
    out: dict[str, dict[str, list[str]]] = {}
    for r in rows:
        media = str(r["content_type"])
        try:
            fmt = format_of(str(r["filename"]), media, str(r["declared_format"]))
        except ValueError:  # refused by name: unsupported_format, never format_unavailable
            continue
        url = extractors.endpoint_for(fmt, media)
        out.setdefault(fmt, {}).setdefault(url, []).append(str(r["source_uri"]))
    return out


def _names(services: list[extractors.Service], urls: set[str]) -> str:
    """The settings naming the services at `urls`; 'no service' for a format none takes."""
    named = sorted({s.setting for s in services if s.url in urls})
    return " and ".join(named) if named else "no service"


def _round_robin(lists: list[list[str]], limit: int) -> list[str]:
    out: list[str] = []
    for depth in range(max((len(x) for x in lists), default=0)):
        out += [x[depth] for x in lists if depth < len(x) and x[depth] not in out]
    return out[:limit]


@dataclass
class _Route:
    """The route drafted for one format."""

    # The service the format would go to: the setting naming it, and its URL.
    setting: str
    url: str
    why: str
    # The failed sources it answers, and whether it answers the format's incomplete
    # extractions too (its service is not the one that made them).
    failed: list[str] = field(default_factory=list)
    incomplete: bool = False


def _lost(row: dict[str, Any]) -> str:
    lost = ", ".join(str(x) for x in row["lost"]) or "units it could not extract"
    noun = "document" if int(row["documents"]) == 1 else "documents"
    return f"{row['incomplete']} of {row['documents']} {noun} extracted lost {lost}"


def _sources(n: int) -> str:
    return f"{n} {'source' if n == 1 else 'sources'} failed format_unavailable"


def _judge_format(
    fmt: str,
    by_url: dict[str, list[str]],
    row: dict[str, Any] | None,
    services: list[extractors.Service],
    routes: dict[str, str],
    skipped: list[str],
) -> _Route | None:
    """The route to draft for `fmt` from its failed sources (`by_url`: the URL each goes
    to now) and its extractions (`row`), or None; what it cannot draft is said in
    `skipped`. A format's own route wins over a media type's, so it takes every source of
    the format: of the services that declare it, the route goes to one its sources
    already work on (extracted whole, or failed sources whose service declares it now),
    else one its media-type routes reach, else any, a service that did not make its
    incomplete extractions first within each."""
    declaring = [s for s in services if s.formats and fmt in s.formats]
    declared = {s.url for s in declaring}

    def served(key: str) -> set[str]:
        return {extractors.endpoint_for(fmt, str(m)) for m in row[key]} if row else set()

    producers, whole = served("incomplete_types"), served("complete_types")
    now = {u for u in by_url if u in declared}
    if now:
        skipped.append(
            f"extractor.{fmt}: {_sources(sum(len(by_url[u]) for u in now))}, and the service "
            "they go to declares it now: upload each original again and knowledge.ingest it"
        )
    stuck = {u: v for u, v in by_url.items() if u not in declared}
    lossy = row is not None and int(row["incomplete"]) > 0
    if not stuck and not lossy:
        return None
    failed = _sources(sum(len(v) for v in stuck.values()))
    # A failed source is answered by any declaring service; an incomplete extraction only
    # by one that did not make it.
    candidates = declaring if stuck else [s for s in declaring if s.url not in producers]
    if not candidates:
        unread = sum(1 for s in services if s.formats is None)
        note = f" ({unread} configured could not be read)" if unread else ""
        what = (
            f"{failed}, and no configured extractor declares {fmt}{note}: deploy one that "
            f"does (the extractor contract) and route {fmt} to it in "
            "LOOM_DOCUMENT_EXTRACTORS_JSON"
            if stuck
            else f"{_lost(row or {})}, and no other configured extractor declares {fmt}{note}"
        )
        skipped.append(f"extractor.{fmt}: {what}")
        return None
    working = whole | now
    reached = {u.rstrip("/") for k, u in routes.items() if format_of_media_type(k) == fmt}
    pick = min(
        candidates,
        key=lambda s: (0 if s.url in working else 1 if s.url in reached else 2, s.url in producers),
    )
    answers_lossy = lossy and pick.url not in producers
    parts = (
        [f"{failed}; they go to {_names(services, set(stuck))}, which does not declare it"]
        if stuck
        else []
    )
    if answers_lossy:
        parts.append(f"{_lost(row or {})} under {_names(services, producers)}")
    elif lossy:
        skipped.append(
            f"extractor.{fmt}: {_lost(row or {})} under {_names(services, producers)}, the "
            "service the route drafted for its failed sources goes to"
        )
    why = f"{fmt}: {'; '.join(parts)}; the service {pick.setting} names declares {fmt}."
    if moved := whole - {pick.url}:
        why += (
            f" The route also sends there the {fmt} sources {_names(services, moved)} "
            "extracts whole now."
        )
    return _Route(
        pick.setting,
        pick.url,
        why,
        failed=[uri for u in sorted(stuck) for uri in stuck[u]],
        incomplete=answers_lossy,
    )


async def _routes(
    inp: NamespaceTuneInput,
    guarded: Guarded,
    skipped: list[str],
    agents: list[str],
    total: int | None,
) -> TuneProposal | None:
    org, ns = inp.org, inp.namespace
    jobs = await guarded(
        "extractor.jobs",
        lambda: inspect_store.format_unavailable_sources(org, ns, lease_s=JOB_LEASE_S),
        None,
    )
    coverage = await guarded(
        "extractor.coverage", lambda: inspect_store.extraction_by_format(org, ns), None
    )
    if jobs is not None and len(jobs) > inspect_store.MAX_FORMAT_SOURCES:
        jobs = jobs[: inspect_store.MAX_FORMAT_SOURCES]
        skipped.append(
            f"extractor.jobs: more than {inspect_store.MAX_FORMAT_SOURCES} sources failed "
            "format_unavailable; the counts cover the first"
        )
    if coverage is not None and len(coverage) > inspect_store.MAX_GROUPS:
        coverage = coverage[: inspect_store.MAX_GROUPS]
        skipped.append(
            f"extractor.coverage: more than {inspect_store.MAX_GROUPS} formats; the counts "
            "cover the first"
        )
    failing = _failing_formats(jobs or [])
    by_format = {str(r["doc_format"]): r for r in coverage or []}
    lossy = {f: r for f, r in by_format.items() if int(r["incomplete"])}
    if not failing and not lossy:
        return None
    services = await guarded(
        "extractor.services",
        lambda: extractors.configured_services(timeout_s=CAPABILITIES_READ_S),
        None,
    )
    routes = await guarded("extractor.routes", _current_routes, None)
    if services is None or routes is None:
        return None
    drafted: dict[str, _Route] = {}
    for fmt in sorted({*failing, *lossy}):
        route = _judge_format(
            fmt, failing.get(fmt, {}), by_format.get(fmt), services, routes, skipped
        )
        if route is not None:
            drafted[fmt] = route
    if not drafted:
        return None
    current = {k: "<as set>" for k in sorted(routes)}
    proposed_map = {**current, **{f: f"<the URL {r.setting} names>" for f, r in drafted.items()}}
    proposed = json.dumps(dict(sorted(proposed_map.items())))
    fmts = sorted(drafted)
    failed = [drafted[f].failed for f in fmts if drafted[f].failed]
    incomplete = [
        [str(u) for u in lossy[f]["incomplete_uris"]] for f in fmts if drafted[f].incomplete
    ]
    complete = [
        int(by_format[f]["documents"]) - int(by_format[f]["incomplete"])
        for f in fmts
        if f in by_format
    ]
    codes = (["extractor_unavailable"] if failed else []) + (
        ["extraction_incomplete"] if incomplete else []
    )
    return TuneProposal(
        id="",
        target="settings.document_extractors_json",
        scope=_SCOPE,
        title=f"Route {', '.join(fmts)} to an extractor that declares "
        + ("it" if len(fmts) == 1 else "them"),
        rationale=" ".join(r.why for r in drafted.values()) + f" Counts are {org}/{ns}'s.",
        current=json.dumps(current),
        proposed=proposed,
        evidence=TuneEvidence(
            finding_codes=codes,
            supporting_count=sum(len(x) for x in failed)
            + sum(int(lossy[f]["incomplete"]) for f in fmts if drafted[f].incomplete),
            documents=_round_robin(failed + incomplete, inp.sample_limit),
            # The documents of these formats extracted whole, which the route must keep.
            counter_count=sum(complete) if coverage is not None else None,
        ),
        affected_agents=agents,
        affected_agents_total=total,
        historical=_HISTORICAL_ROUTES,
        apply=TuneApply(
            op="LOOM_DOCUMENT_EXTRACTORS_JSON",
            via="environment",
            args={"LOOM_DOCUMENT_EXTRACTORS_JSON": json.loads(proposed)},
            args_complete=False,
            required_role="platform_admin",
            guard=(
                "None: an environment setting has no revision; compare the deployment's "
                "value with `current` before setting it. It is in force once each server "
                "and worker restarts."
            ),
        ),
        limits=(
            "A URL is not shown, as an extractor URL can carry a credential: each "
            "placeholder names the setting whose URL it is, and `<as set>` keeps a route as "
            "the deployment has it. A service's capabilities name the formats it takes, not "
            "what it keeps: ingest one listed source through the new route and read its "
            "coverage (knowledge.inspect) before relying on it. A format's own route wins "
            "over a media-type route (`application/pdf`), so the drafted route also takes "
            "the sources a media-type route sends elsewhere now."
        ),
    )


async def _current_routes() -> dict[str, str]:
    return settings.document_extractor_routes()


async def document_drafts(
    inp: NamespaceTuneInput, guarded: Guarded, skipped: list[str]
) -> DocumentDrafts:
    """The chunking and extractor drafts for `inp`'s namespace, with the chunk sample."""
    org, ns = inp.org, inp.namespace
    out = DocumentDrafts()
    groups = await guarded(
        "documents.population", lambda: inspect_store.document_groups(org, ns), None
    )
    population = None
    if groups is not None and len(groups) <= inspect_store.MAX_GROUPS:
        population = sum(int(g["live"]) for g in groups)
    writers = await guarded(
        "documents.writers",
        lambda: store.document_writers(org, ns, limit=MAX_AFFECTED_AGENTS),
        None,
    )
    agents = [str(r["agent"]) for r in writers or []]
    total = None if writers is None else (int(writers[0]["total"]) if writers else 0)
    out.chunking, chunking = await _chunking(inp, guarded, skipped, population, agents, total)
    if chunking is not None:
        out.proposals.append(chunking)
    routes = await _routes(inp, guarded, skipped, agents, total)
    if routes is not None:
        out.proposals.append(routes)
    if population or out.chunking is not None:
        skipped.append(DECISIONS_SKIPPED)
    return out
