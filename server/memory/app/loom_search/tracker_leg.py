"""Bounded issue and pull-request search over authorized tracker records.

Targets are resolved before parallel search so every leg inherits their capture
consent. General questions get cited similar items; defect reports under a code
scope keep the duplicate-check verdict and its linked fixes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from server.tracker.app.dupcheck import DuplicateCheckPlan

logger = logging.getLogger(__name__)

TRACKER_LEG_TOP_K = 5
# How the nearest rows are introduced — never as matches
NEAREST_NOTE = "nearest reported issues, below the related line (NOT duplicates — check by eye)"
# a nearest row in the search answer: enough to recognise it (the url is
# `#number` on the record's repo; `loom_tracker_duplicate_check` carries it)
_NEAREST_KEEP = (
    "namespace",
    "number",
    "title",
    "state",
    "confidence",
    "rerank",
    "links",
    "fixed_in",
)
NEAREST_TITLE_CHARS = 72

_FRAME_RE = re.compile(r"\b[A-Za-z_][\w]*(?:::[~A-Za-z_][\w]*)+")
_ERROR_RE = re.compile(
    r"\b(LOGICAL_ERROR|[A-Z][A-Z_]{5,}_(?:ERROR|EXCEPTION|FAILED|VIOLATION)|Code: \d{2,5}|"
    r"DB::Exception|std::exception|segfault|SIGSEGV|SIGABRT|core dump|stack trace|assertion failed|"
    r"Sanitizer|UBSan|ASan|MSan|TSan|flaky|crash(?:es|ing|ed)?|segfaults?)\b",
    re.IGNORECASE,
)
# An error NAME is an error signal too — the ErrorCodes
# spelling (`ILLEGAL_TYPE_OF_ARGUMENT`, `TOO_MANY_PARTS`) and its CamelCase
# form (`IllegalTypeOfArgument`). Case-sensitive, unlike _ERROR_RE.
_ERROR_NAME_RE = re.compile(
    r"\b(?:[A-Z]{2,}(?:_[A-Z]{2,}){2,}|"
    r"(?:Illegal|Bad|Cannot|Unknown|Logical|Invalid|Unexpected|Incorrect|Unsupported)"
    r"[A-Z][a-z]+(?:[A-Z][a-z]+)+)\b"
)
_INTENT_RE = re.compile(
    r"\b(already (?:reported|filed|known|exists?)|is this (?:a )?known|known (?:issue|bug)|"
    r"duplicate|dup check|before i file|file (?:a |an |this )?(?:new )?(?:issue|bug)|"
    r"been (?:reported|seen|filed)|reported before|seen before|open issue for)\b",
    re.IGNORECASE,
)

# fields of a duplicate-check item the reader acts on; the rest is the fit's bookkeeping
_ITEM_KEEP = (
    "namespace",
    "kind",
    "number",
    "title",
    "state",
    "url",
    "confidence",
    "relation",
    "why",
    # The linked fix / in-progress PR and the release
    "links",
    "fixed_in",
    "fixed_in_source",
    "backported_to",
)


def defect_report_shape(query: str) -> dict[str, Any] | None:
    """{title, body, frames, signals} when `query` reads like a defect report
    or a question about one; None for the questions memory answers.
    Two of three signals (a frame, an error/exception token, the asking
    intent) make the shape; a lone frame with a question about definitions
    ('where is X::y defined') is a code question and stays out."""
    text = str(query or "").strip()
    if len(text) < 12:
        return None
    from server.tracker.api.models import within_item_bound

    frames = within_item_bound(dict.fromkeys(m.group(0) for m in _FRAME_RE.finditer(text)))
    signals = [
        s
        for s, hit in (
            ("frames", bool(frames)),
            ("error", bool(_ERROR_RE.search(text) or _ERROR_NAME_RE.search(text))),
            ("intent", bool(_INTENT_RE.search(text))),
        )
        if hit
    ]
    if len(signals) < 2:
        return None
    # The check reads the report, not the question about it: a kept
    # "— already reported?" tail would defeat the same-title gate.
    from server.tracker.domain.defect_answer import strip_intent

    report = strip_intent(text) or text
    first, _, rest = report.partition("\n")
    title = first.strip()[:300] if len(first.strip()) >= 12 else report[:300]
    return {"title": title, "body": rest.strip()[:8000], "frames": frames[:60], "signals": signals}


def lean_tracker_section(out: Any, shape: dict[str, Any]) -> dict[str, Any]:
    """The `qa` section of a loom_search answer: the verdict, the records
    read, and the items a reader acts on."""
    dump = out.model_dump() if hasattr(out, "model_dump") else dict(out or {})
    items = [
        {k: it.get(k) for k in _ITEM_KEEP if it.get(k) not in (None, "", [], {})}
        for it in (dump.get("items") or [])
        if isinstance(it, dict)
    ]
    section: dict[str, Any] = {
        "verdict": str(dump.get("verdict") or "none"),
        "epistemic": str(dump.get("epistemic") or ""),
        "undecided": bool(dump.get("undecided")),
        "namespaces": list(dump.get("namespaces") or []),
        "signals": list(shape.get("signals") or []),
        "results": items,
    }
    # The one sentence (already reported? fixed? in which release?)
    if dump.get("answer"):
        section["answer"] = str(dump["answer"])
    # A same-titled record the fit scored out, and what to pass
    closest = [
        {k: it.get(k) for k in _ITEM_KEEP if it.get(k) not in (None, "", [], {})}
        for it in (dump.get("closest") or [])
        if isinstance(it, dict)
    ]
    if closest:
        section["closest"] = closest
        section["note"] = str(dump.get("closest_note") or "")
    # The nearest on-topic issues under the related line
    nearest = [
        {
            k: (str(it[k])[:NEAREST_TITLE_CHARS] if k == "title" else it[k])
            for k in _NEAREST_KEEP
            if it.get(k) not in (None, "", [], {})
        }
        for it in (dump.get("nearest") or [])
        if isinstance(it, dict)
    ]
    if nearest:
        section["nearest"] = nearest
        section["nearest_note"] = NEAREST_NOTE
    return section


@dataclass(frozen=True)
class TrackerSearchPlan:
    org: str
    namespaces: tuple[str, ...]
    deadline: float
    namespace_limit: int
    duplicate: DuplicateCheckPlan | None = None


async def prepare_tracker_search(
    query: str, *, org: str, top_k: int = TRACKER_LEG_TOP_K, timeout_s: float | None = None
) -> TrackerSearchPlan | None:
    """Resolve authorized targets before sibling legs inherit capture consent."""
    from server.code.domain.scope import current_code_scope
    from server.identity.app.request_scope import request_scope
    from server.settings import settings
    from server.tracker.app.dupcheck_fanout import readable_tracker_namespaces

    if request_scope().narrowed:
        return None
    deadline = asyncio.get_running_loop().time() + (
        settings.mcp_loom_search_tracker_timeout_s if timeout_s is None else timeout_s
    )
    cap = max(1, min(settings.tracker_dupcheck_fanout_namespaces, 8))
    try:
        async with asyncio.timeout_at(deadline):
            scope = current_code_scope()
            shape = defect_report_shape(query)
            if scope and shape:
                from server.tracker.api.models import TrackerDuplicateCheckInput
                from server.tracker.app.dupcheck import prepare_duplicate_check

                duplicate = await prepare_duplicate_check(
                    TrackerDuplicateCheckInput(
                        org=scope[0],
                        namespace=scope[1],
                        title=shape["title"],
                        body=shape["body"],
                        frames=shape["frames"],
                        top_k=max(1, min(int(top_k), 10)),
                        related_prs=False,
                    )
                )
                if duplicate.targets:
                    return TrackerSearchPlan(scope[0], duplicate.targets, deadline, cap, duplicate)
            namespaces = await readable_tracker_namespaces(org, "", [], cap)
            return TrackerSearchPlan(org, tuple(namespaces), deadline, cap) if namespaces else None
    except Exception as e:  # noqa: BLE001 — fail open: the memory legs answer
        logger.info("loom_search tracker discovery skipped on %s: %s", org, type(e).__name__)
        return None


async def tracker_leg(
    query: str, *, plan: TrackerSearchPlan | None, top_k: int = TRACKER_LEG_TOP_K
) -> dict[str, Any] | None:
    """Search the prepared targets within the original discovery-and-read deadline."""
    if plan is None or asyncio.get_running_loop().time() >= plan.deadline:
        return None
    try:
        async with asyncio.timeout_at(plan.deadline):
            if plan.duplicate is None:
                return await _similar_section(plan, query, top_k)
            from server.tracker.app.dupcheck import execute_duplicate_check

            out = await execute_duplicate_check(plan.duplicate)
            if out.tracker_unavailable:
                return None
            return lean_tracker_section(out, defect_report_shape(query) or {})
    except Exception as e:  # noqa: BLE001 — fail open: the memory legs answer
        logger.info("loom_search tracker leg skipped on %s: %s", plan.org, type(e).__name__)
        return None


async def _similar_section(
    plan: TrackerSearchPlan, query: str, top_k: int
) -> dict[str, Any] | None:
    from server.tracker.api.models import TrackerSimilarInput
    from server.tracker.app.similar import similar_in_namespace

    limit = max(1, min(top_k, 50))
    outputs = await asyncio.gather(
        *(
            similar_in_namespace(
                TrackerSimilarInput(
                    org=plan.org,
                    namespace=ns,
                    text=query[:20000],
                    top_k=limit,
                    # General search neither generates embeddings nor catches up the vector index.
                    kinds=["issue", "pr"],
                    legs=["lexical", "fields", "frames", "mentions"],
                )
            )
            for ns in plan.namespaces
        ),
        return_exceptions=True,
    )
    results: list[dict[str, Any]] = []
    served: list[str] = []
    for namespace, output in zip(plan.namespaces, outputs, strict=True):
        if isinstance(output, BaseException) or output.tracker_unavailable:
            continue
        served.append(namespace)
        for row in output.items:
            if float(row.get("score") or 0) <= 0:
                continue
            results.append(
                {
                    "namespace": namespace,
                    **{
                        field: row[field]
                        for field in ("kind", "number", "state", "score")
                        if field in row
                    },
                    "title": str(row.get("title") or "")[:300],
                    "url": str(row.get("url") or "")[:2048],
                }
            )
    results.sort(
        key=lambda row: (
            -float(row["score"]),
            row["namespace"],
            row.get("kind", ""),
            int(row.get("number") or 0),
        )
    )
    if not results:
        return None
    return {
        "mode": "search",
        "namespaces": served,
        "namespace_limit": plan.namespace_limit,
        "results": results[:limit],
    }


def tracker_text_lines(section: dict[str, Any] | None, top_k: int = TRACKER_LEG_TOP_K) -> list[str]:
    """The text block's opening lines for a `qa` section: the verdict in one
    sentence, then the items. Empty when there is no section."""
    if not section:
        return []
    if section.get("mode") == "search":
        return [
            "Related tracker issues and pull requests:",
            *_item_lines((section.get("results") or [])[:top_k]),
            "",
        ]
    verdict = section.get("verdict") or "none"
    read = " + ".join(section.get("namespaces") or []) or "the tracker record"
    items = section.get("results") or []
    if verdict in ("duplicate_of", "duplicate"):
        head = (
            f"tracker record ({read}): this looks ALREADY REPORTED — "
            f"the same defect ({len(items)}):"
        )
    elif items:
        head = (
            f"tracker record ({read}): no likely duplicate in the indexed records "
            f"(verdict {verdict}); closest reported items ({len(items)}):"
        )
    elif section.get("closest"):
        items = section["closest"]
        head = (
            f"tracker record ({read}): no duplicate by the fit (verdict none), but the same "
            f"title is already filed — {section.get('note') or 'check it before filing'}:"
        )
    elif section.get("nearest"):
        head = (
            f"tracker record ({read}): no likely duplicate in the indexed records "
            f"(verdict none, {section.get('epistemic') or 'calibrated'}); {NEAREST_NOTE}:"
        )
    else:
        head = (
            f"tracker record ({read}): no likely duplicate in the indexed records "
            f"(verdict none, {section.get('epistemic') or 'calibrated'})."
        )
    lines = [head]
    if section.get("answer"):
        lines.append(f"  {section['answer']}")
    lines += _item_lines(items[:top_k])
    nearest = section.get("nearest") or []
    if nearest and (items or section.get("closest")):
        lines.append(f"  {NEAREST_NOTE}:")
    lines += _item_lines(nearest, digits=3)
    lines.append("")
    return lines


def _item_lines(items: list[dict[str, Any]], digits: int = 2) -> list[str]:
    lines: list[str] = []
    for it in items:
        conf = it.get("confidence")
        tail = ", ".join(
            x
            for x in (
                str(it.get("state") or ""),
                f"{conf:.{digits}f}" if isinstance(conf, int | float) else "",
            )
            if x
        )
        rr = it.get("rerank") if isinstance(it.get("rerank"), dict) else None
        if rr and rr.get("label"):
            # The rerank's reading, never a verdict
            said = f"model: {rr['label']} {rr.get('confidence', '')}".strip()
            tail = ", ".join(x for x in (tail, said) if x)
        ns = f"[{it['namespace']}] " if it.get("namespace") else ""
        where = f" ({tail})" if tail else ""
        url = f" {it['url']}" if it.get("url") else ""
        lines.append(f"  - {ns}#{it.get('number', '?')} {it.get('title', '')}{where}{url}")
    return lines
