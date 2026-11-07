"""code.ask — the cross-agent grounded answer cache.

Different agents ask the SAME conceptual questions about a codebase over
and over; each normally pays retrieval + synthesis. On a shared store the
synthesis is memoizable:

    question --embed--> nearest cached answer (cosine >= threshold)
        hit  -> verify every cited file's content hash is still current
                (a merge that touched a cited file invalidates the entry —
                 a stale answer is worse than a slow one) -> serve, free
        miss -> hybrid retrieval -> grounded LLM synthesis WITH citations
                -> cache -> serve

Abstention flows through: if retrieval abstains, code.ask abstains — no
synthesis, nothing cached. The cache is per (org, namespace,
embedding_model); entries are RMT rows tombstoned on invalidation.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, NamedTuple

from server.code.app.answers_cache import (
    _exact_cached,
    _pre_synthesis_cache_on,
    _remember,
    _request_cost,
    _serve_from_cache,
)
from server.code.app.answers_entry import (
    _EXCERPT_CHARS,
    _EXPAND_SYSTEM,
    _HISTORY_NOTE,
    _MAX_CONTEXT_UNITS,
    _MAX_EXPANSION_UNITS,
    _MEMORY_NOTE,
    _SYNTH_SYSTEM,
    AskResult,
    _AskCtx,
    _synth_model_of,
    cache_key_for,
    cache_namespace_ok,
)
from server.code.app.search import CodeSearchResult
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.observability.genai import staged
from server.settings.budgets import llm_max_tokens

logger = logging.getLogger(__name__)

# PR / issue numbers of the indexed repository (4-7 digits, the ClickHouse range) —
# "#119663", "PR 119663", "issue #119651".
_PR_OR_ISSUE_RE = re.compile(r"(?:#|\b(?:PR|pull request|issue)\s*#?)\s*(\d{4,7})\b", re.IGNORECASE)


def names_pr_or_issue(question: str) -> bool:
    """True when the question names a PR or issue number of this repository."""
    return bool(_PR_OR_ISSUE_RE.search(question or ""))


_QUESTION_STOPWORDS = frozenset(
    "how does do the a an are is where what when why in on of for to from "
    "with and or get set old new".split()
)


async def ask(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    question: str,
    embedder: Any,
    llm: Any,
    search_kwargs: dict[str, Any],
    allow_synthesis: bool = True,
    no_cache: bool = False,
    index_head: str = "",
) -> AskResult:
    """Answer `question` from the cache, else retrieve and synthesize.

    The lookup runs before any LLM call, so a hit pays for no identifier proposal; a hit
    must match the complete cache key, pass the identifier guard and find every cited
    and shown file unchanged; an abstain is cached briefly for its exact text under
    `index_head`; a release branch reuses its trunk's entry when the files agree.
    `no_cache` skips the reads (the fresh answer still refreshes the entry); an
    unresolved namespace, or a caller whose memory read is narrowed, skips reads and
    writes."""
    from server.llm.cache import exact_cache_scope

    qvec = await embedder.embed(question)
    ctx = _AskCtx(
        repo=repo,
        org=org,
        ns=ns,
        question=question,
        qvec=qvec,
        model=embedder.model_name,
        key=cache_key_for(search_kwargs, _synth_model_of(llm)),
        index_head=(index_head or "").strip(),
        started=time.monotonic(),
        cost_at_start=_request_cost(),
    )
    cacheable = cache_namespace_ok(ns) and not _memory_read_narrowed()
    outcome = "bypassed"
    if cacheable and not no_cache:
        looked = await _serve_from_cache(ctx)
        if looked.result is not None:
            return looked.result
        outcome = looked.outcome
    llm_cache_on = cacheable and not no_cache and _pre_synthesis_cache_on()
    scope = exact_cache_scope(org, ns) if llm_cache_on else contextlib.nullcontext()
    with scope:
        res = await _answer_fresh(
            ctx,
            embedder=embedder,
            llm=llm,
            search_kwargs=search_kwargs,
            allow_synthesis=allow_synthesis,
            cache_llm=llm_cache_on,
        )
    res.cache = outcome
    if cacheable:
        await _remember(ctx, res, llm)
    return res


async def _answer_fresh(
    ctx: _AskCtx,
    *,
    embedder: Any,
    llm: Any,
    search_kwargs: dict[str, Any],
    allow_synthesis: bool,
    cache_llm: bool,
) -> AskResult:
    """The miss path: identifier proposal, off-index gate, retrieval,
    grounded synthesis. Nothing here reads or writes the answer cache."""
    from server.code.app.search import search as run_search

    repo, org, ns, question = ctx.repo, ctx.org, ctx.ns, ctx.question
    # ---- vocabulary bridge: propose codebase identifiers for the lexical
    # leg. Natural-language questions collide with the wrong subsystem's vocabulary
    # ("replication log" -> MergeTree instead of Keeper's raft Changelog); asking the
    # LLM for likely identifiers first lets BM25 find the actual implementing symbols.
    # Cache misses only; fail-open. Behind the exact-prompt response cache (per
    # namespace).
    proposed: list[str] = []
    if llm is not None and "extra_terms" not in search_kwargs:
        proposer = _exact_cached(llm, org, ns) if cache_llm else llm
        proposed = await _propose_identifiers(proposer, question)
        search_kwargs = {
            **search_kwargs,
            "extra_terms": [] if proposed == ["NONE"] else proposed,
        }

    proposer_off_index = proposed == ["NONE"] and not names_pr_or_issue(question)

    # ---- retrieval ----------------------------------------------------------
    retrieval = await run_search(repo, org, ns, query=question, embedder=embedder, **search_kwargs)
    # The history route's PR / issue records are evidence the synthesizer reads, not a
    # side section only code.search callers see. A number question with no anchored code
    # excerpt is still answerable from them, so an empty unit list abstains only when
    # the history is empty too.
    history = history_lines(getattr(retrieval, "history", None) or {})
    # NONE is only a proposal, not stronger evidence than the repository index. This
    # extra check costs retrieval only for suspected off-repository questions and keeps
    # a repository-specific expansion prompt from rejecting another indexed stack.
    if proposer_off_index and not retrieval.units and not history:
        return AskResult(
            source="abstained",
            abstain=True,
            abstain_reason=(
                "this question appears to concern a different technology "
                "than the indexed codebase; use grep or ask about this "
                "repository"
            ),
            retrieval=retrieval,
        )
    if retrieval.abstain and not history or (not retrieval.units and not history):
        return AskResult(
            source="abstained",
            abstain=True,
            abstain_reason=retrieval.abstain_reason or "no strong match in the code index",
            retrieval=retrieval,
        )
    if not allow_synthesis or llm is None:
        return AskResult(
            source="synthesized",
            retrieval=retrieval,
            abstain=False,
            answer="",
            citations=_citations_of(retrieval.units),
        )
    return await _grounded_answer(ctx, llm, retrieval, history)


async def _grounded_answer(
    ctx: _AskCtx, llm: Any, retrieval: CodeSearchResult, history: list[str]
) -> AskResult:
    repo, org, ns, question = ctx.repo, ctx.org, ctx.ns, ctx.question
    units = retrieval.units[:_MAX_CONTEXT_UNITS]
    if units:
        units = units + await _expand_mechanism(repo, org, ns, question, units, ctx.model)
    memory = await _inject_memory_context(question)
    answer = await _synthesize(
        llm,
        question,
        units,
        memories=[f"[{m.label}] {m.text}" if m.label else m.text for m in memory.items],
        history=history,
    )
    # Cite what the answer USED, not everything it was shown: context units
    # the model ignored are noise to the reader (and judged as such).
    citations = _citations_of(_units_cited(answer, units) or units)

    # ---- cache gate: the synthesizer self-grades coverage (not the retrieval
    # risk score, which reflects the embedder's compressed cosine scale, not answer
    # quality). "COVERAGE: none" answers are returned to the caller once
    # but NEVER memoized: a cached junk answer is served forever. An answer grounded in
    # a non-public memory namespace is served to this caller and never memoized for the
    # next one. A memoized one names the memory it quoted, for the erasure saga.
    answer, coverage = _split_coverage(answer)
    memoize = coverage != "none" and await _memory_context_cacheable(org, memory)
    return AskResult(
        answer=answer,
        citations=citations,
        source="synthesized",
        retrieval=retrieval,
        tokens_returned=len(answer) // 4,
        coverage=coverage,
        memoize=memoize,
        # the files the synthesis was SHOWN: an entry is stale when any of them changes,
        # cited or not
        shown_paths=list(dict.fromkeys(u.path for u in units)),
        memory_namespace=memory.namespace if memory.items else "",
        memory_keys=list(dict.fromkeys(m.erasure_key for m in memory.items)),
    )


async def _expand_mechanism(
    repo: CodeRepo,
    org: str,
    ns: str,
    question: str,
    units: list[CodeUnit],
    model: str,
) -> list[CodeUnit]:
    """Retrieval can land the right FILE while the implementing function's
    chunk ranks below the context cutoff. Recover it structurally: score
    every function symbol defined in the retrieved files by question-subtoken
    overlap and pull the best ones' whole chunks into the synthesis window.
    Purely additive and bounded; any failure degrades to no expansion."""
    try:
        from server.code.domain.extract.tokenize import code_tokens

        qtok = {t for t in code_tokens(question) if len(t) > 2 and t not in _QUESTION_STOPWORDS}
        if not qtok:
            return []
        paths = list(dict.fromkeys(u.path for u in units))[:8]
        covered = {(u.path, u.symbol) for u in units}
        syms = await repo.functions_for_paths(org, ns, paths)
        scored: list[tuple[float, dict[str, Any]]] = []
        for s in syms:
            ntok = set(code_tokens(str(s["name"])))
            overlap = len(ntok & qtok)
            if overlap >= 2 and (str(s["path"]), str(s["qualified_name"])) not in covered:
                scored.append((overlap + min(overlap / max(len(ntok), 1), 0.99), s))
        scored.sort(key=lambda t: -t[0])
        out: list[CodeUnit] = []
        seen_chunks = {u.unit_id for u in units if u.unit_id}
        for _, s in scored[: _MAX_EXPANSION_UNITS * 2]:
            if len(out) >= _MAX_EXPANSION_UNITS:
                break
            rows = await repo.live_chunks_for_path(org, ns, str(s["path"]), model)
            best = None
            for row in rows:
                if str(row.get("scope")) == str(s["qualified_name"]) or (
                    int(row.get("start_line") or 0) <= int(s["start_line"])
                    and int(row.get("end_line") or 0) >= int(s["end_line"])
                ):
                    best = row
                    break
            if best is None or not str(best.get("content") or "").strip():
                continue
            if str(best.get("chunk_id") or "") in seen_chunks:
                continue
            seen_chunks.add(str(best.get("chunk_id") or ""))
            out.append(
                CodeUnit(
                    unit_id=str(best.get("chunk_id") or ""),
                    path=str(s["path"]),
                    start_line=int(best.get("start_line") or 0),
                    end_line=int(best.get("end_line") or 0),
                    symbol=str(s["qualified_name"]),
                    kind="mechanism_expansion",
                    signature=str(s.get("signature") or ""),
                    body=str(best.get("content") or ""),
                )
            )
            covered.add((str(s["path"]), str(s["qualified_name"])))
        return out
    except Exception as e:  # noqa: BLE001 — expansion must never sink an answer
        logger.debug("mechanism expansion skipped: %s", e)
        return []


@staged("code_query_expand")
async def _propose_identifiers(llm: Any, question: str) -> list[str]:
    try:
        res = await llm.complete(
            _EXPAND_SYSTEM,
            question,
            max_tokens=llm_max_tokens("code_answer_identifiers"),
        )
        text = str(res.text).strip()
        if text.upper().startswith("NONE"):
            return ["NONE"]
        terms = [t.strip("`,.") for t in text.split()]
        return [t for t in terms if t.replace("_", "").replace("::", "").isalnum()][:6]
    except Exception as e:  # noqa: BLE001 — expansion is best-effort
        logger.debug("identifier proposal skipped: %s", e)
        return []


def history_lines(history: dict[str, Any]) -> list[str]:
    """The history section of a code.search result as one-line records for the
    synthesizer: merged PRs with author, merge date, title, linked issues and role;
    items with kind, state, opener and title. Unknown numbers render nothing."""
    out: list[str] = []
    for pr in list(history.get("prs") or []):
        n = int(pr.get("pr_number") or 0)
        if n <= 0:
            continue
        parts = [f"PR #{n}"]
        if pr.get("author"):
            parts.append(f"by @{pr['author']}")
        merged = str(pr.get("merged_at") or "")[:10]
        parts.append(f"merged {merged}" if merged else "merged")
        if pr.get("title"):
            parts.append(f"— {str(pr['title'])[:160]}")
        linked = [int(x) for x in (pr.get("linked_issues") or []) if int(x) > 0]
        if linked:
            plural = "s " if len(linked) > 1 else " "
            parts.append("links issue" + plural + ", ".join(f"#{i}" for i in linked))
        role = str(pr.get("role") or "")
        if role and role != "pr":
            parts.append(f"({role})")
        if pr.get("is_revert"):
            parts.append("(a revert)")
        out.append(" ".join(parts))
    for it in list(history.get("items") or []):
        n = int(it.get("number") or 0)
        kind = str(it.get("kind") or "unknown")
        if n <= 0 or kind == "unknown":
            continue
        parts = [f"#{n} is a{'n' if kind[0] in 'aeiou' else ''} {kind}"]
        state = str(it.get("state") or "")
        reason = str(it.get("state_reason") or "")
        if state:
            parts.append(f"({state}{', ' + reason if reason else ''})")
        opened = str(it.get("opened_at") or "")[:10]
        if opened:
            parts.append(f"opened {opened}")
        if it.get("author"):
            parts.append(f"by @{it['author']}")
        if it.get("title"):
            parts.append(f"— {str(it['title'])[:160]}")
        out.append(" ".join(parts))
        # The tracker record beside the item — what a reviewer checks first (bisect
        # verdict, branches still affected, test presence, fixes / backports).
        qa = it.get("tracker")
        if isinstance(qa, dict) and qa:
            from server.tracker.app.facts import tracker_fact_line

            line = tracker_fact_line(n, qa)
            if line:
                out.append(line)
    return out[:12]


@staged("code_answer")
async def _synthesize(
    llm: Any,
    question: str,
    units: list[CodeUnit],
    memories: list[str] | None = None,
    history: list[str] | None = None,
) -> str:
    excerpts = "\n\n".join(
        f"[{u.path}:{u.start_line}-{u.end_line}] {u.symbol}{u.signature}\n{u.body[:_EXCERPT_CHARS]}"
        for u in units
    )
    prompt = f"Question: {question}\n\nCode excerpts:\n\n{excerpts or '(none)'}"
    if history:
        # The repository's own PR / issue records are authoritative for "was it merged",
        # "who opened it", "which issue does it link to" — questions the code text
        # cannot answer.
        joined = "\n".join(f"- {h}" for h in history)
        prompt += _HISTORY_NOTE + joined
    if memories:
        # Reverse enrichment: the code index shows WHAT the code is; the memory
        # plane holds WHY (the decisions, PR rationale, prior incidents) — which
        # the code alone cannot answer. Offer it as clearly-labeled background,
        # never as a substitute for the cited code.
        joined = "\n".join(f"- {m}" for m in memories)
        prompt += _MEMORY_NOTE + joined
    res = await llm.complete(_SYNTH_SYSTEM, prompt, max_tokens=llm_max_tokens("code_answer_synth"))
    return str(res.text).strip()[:8000]


class MemoryExcerpt(NamedTuple):
    """One excerpt shown to the synthesis: `label` the hit key the prompt names it by,
    `erasure_key` the loom.memory key an erasure finds it by ('' for none)."""

    label: str
    text: str
    erasure_key: str


@dataclass(frozen=True)
class MemoryContext:
    """The memory excerpts an ask showed its synthesis, all from the bound memory
    namespace `namespace` of `org`, read once per ask."""

    org: str = ""
    namespace: str = ""
    items: tuple[MemoryExcerpt, ...] = ()


async def _inject_memory_context(question: str) -> MemoryContext:
    """Decision/rationale memories for a code question — the reverse of
    memory.ask's code enrichment. Gated by an explicit memory-namespace binding
    (code_ask_memory_namespace); a no-op when unset. Returns short excerpts with
    their keys to ground code.ask's synthesis in WHY the code is the way it is.
    Fail-open to no excerpts."""
    from server.settings import settings as _s

    mem_ns = _s.code_ask_memory_namespace.strip()
    if not mem_ns:
        return MemoryContext()
    mem_org = (_s.code_ask_memory_org or _s.code_org or "").strip()
    # The bound memory namespace is read as the CURRENT principal, who was authorized
    # against the code namespace only. No right to read it → no enrichment, never a 403
    # on the ask.
    try:
        from server.auth.authz import row_in_scope
        from server.operations.base import Role

        if not await row_in_scope(mem_org, mem_ns, Role.VIEWER, hides_answer=False):
            return MemoryContext()
    except Exception as e:  # noqa: BLE001 — fail closed
        logger.debug("code.ask memory enrichment authorization failed: %s", e)
        return MemoryContext()
    shown = 3  # memory lines, one per key
    try:
        from server.memory.api.models_search import MemorySearchInput
        from server.memory.app.search import handle_memory_search
        from server.memory.app.search.scoring import one_per_key, per_key_fetch

        # The bound namespace only, whatever the deployment's default scope: an excerpt
        # of the caller's other namespaces would reach every reader of the code cache.
        out = await handle_memory_search(
            MemorySearchInput(
                org=mem_org,
                namespace=mem_ns,
                query=question[:2000],
                top_k=per_key_fetch(shown),
                namespace_scope="this",
                include_tags=True,  # a consolidated row is told by its tags
            )
        )
    except Exception as e:  # noqa: BLE001 — additive, never fatal
        logger.debug("code.ask memory enrichment failed: %s", e)
        return MemoryContext()
    if getattr(out, "abstain", False):
        return MemoryContext()
    # a hit names its namespace only on a cross-namespace search; one from another
    # namespace is dropped all the same
    hits = [
        r
        for r in getattr(out, "results", None) or []
        if (getattr(r, "namespace", "") or mem_ns) == mem_ns
    ]
    # a fact whose tags the search could not read reads as [], like an untagged one
    tags_read = not (getattr(out, "retrieval_work", None) or {}).get("tags_unread")
    items: list[MemoryExcerpt] = []
    for r in one_per_key(hits, mem_ns, shown):
        text = (getattr(r, "content_excerpt", "") or "").strip()[:400]
        if text:
            label = str(getattr(r, "memory_key", "") or "")
            items.append(MemoryExcerpt(label, text, _erasure_key(r, tags_read=tags_read)))
    return MemoryContext(org=mem_org, namespace=mem_ns, items=tuple(items))


def _erasure_key(hit: Any, *, tags_read: bool) -> str:
    """The loom.memory key a user erasure or a redact finds the excerpt by: a fact's own
    key, a chunk's parent fact's. None ('', which keeps an answer quoting it out of the
    shared cache) for a summary, an entity summary, a digest, a chunk with no parent, and
    a fact written from other memories (`consolidated` or a `src:` tag): that row stands
    under another identity, so a user erasure does not reach its key. A fact whose tags
    were not read (`tags_read` false, or none on the hit) counts as one of those."""
    source = str(getattr(hit, "source", "fact") or "fact")
    if source == "fact":
        tags = getattr(hit, "tags", None)
        if (
            not tags_read
            or tags is None
            or any(t == "consolidated" or str(t).startswith("src:") for t in tags)
        ):
            return ""
        return str(getattr(hit, "memory_key", "") or "")
    if source == "chunk":
        return str(getattr(hit, "parent_memory_key", "") or "")
    return ""


def _memory_read_narrowed() -> bool:
    """Whether memory enrichment reads below the whole namespace for this caller. The
    answer cache is shared by every reader of the code namespace and its key has no user
    or session, so such an ask neither reads nor writes it, even when the memory
    namespace is public: the token's narrower ceiling still applies."""
    from server.identity.app.request_scope import request_narrowed
    from server.settings import settings as _s

    return bool(_s.code_ask_memory_namespace.strip()) and request_narrowed()


async def _memory_context_cacheable(org: str, memory: MemoryContext) -> bool:
    """Whether an answer asked in `org` that showed `memory` may be memoized in
    `code_answers`. The cache is per code namespace and is served to every reader of
    it; excerpts from the bound memory namespace may ride along only when that
    namespace is standard AND public — i.e. readable by every principal who can read
    the code namespace — in the same org, where an erasure of the memory finds the
    entry, and when every excerpt has the key an erasure finds it by. Unknown (lookup
    failed) counts as not cacheable."""
    if not memory.items:
        return True
    mem_org, mem_ns = memory.org, memory.namespace
    if mem_org != org or not all(m.erasure_key for m in memory.items):
        return False
    try:
        from server.identity.app.namespace_classification import is_restricted_cached
        from server.identity.app.namespace_visibility import visibility_map

        if is_restricted_cached(mem_org, mem_ns):
            return False
        vis = await visibility_map(mem_org, [mem_ns])
        return vis.get(mem_ns) == "public"
    except Exception:  # noqa: BLE001 — visibility unknown: kept out of the shared cache
        return False


def _split_coverage(answer: str) -> tuple[str, str]:
    """Strip the trailing self-grade line; default to 'partial' when the
    model forgot it (cache rather than lose a probably-fine answer)."""
    lines = answer.rstrip().splitlines()
    if lines and lines[-1].strip().upper().startswith("COVERAGE:"):
        grade = lines[-1].split(":", 1)[1].strip().lower()
        body = "\n".join(lines[:-1]).rstrip()
        return body, grade if grade in ("full", "partial", "none") else "partial"
    return answer, "partial"


_CITE_MARKER_RE = re.compile(r"\[([^\[\]:]+):(\d+)")


def _units_cited(answer: str, units: list[CodeUnit]) -> list[CodeUnit]:
    """The context units whose [path:start-end] markers appear in the
    answer text, in context order. Empty when no marker matches (caller
    falls back to citing everything rather than nothing)."""
    marked = {(m.group(1).strip(), int(m.group(2))) for m in _CITE_MARKER_RE.finditer(answer)}
    if not marked:
        return []
    return [
        u
        for u in units
        if any(
            (p.endswith(u.path) or u.path.endswith(p)) and u.start_line <= ln <= u.end_line
            for p, ln in marked
        )
    ]


def _citations_of(units: list[CodeUnit]) -> list[dict[str, Any]]:
    return [
        {
            "path": u.path,
            "start_line": u.start_line,
            "end_line": u.end_line,
            "symbol": u.symbol,
            "unit_id": u.unit_id,
        }
        for u in units
    ]
