"""The loom_search fan-out: one question over every plane that could answer it.

Memory (plus the scoped repository's decisions), knowledge (plus the scoped
repository's documents), profile, conversations and authorized tracker records
are searched in parallel after their capture consent is resolved; the
sections are assembled into one payload and trimmed by the answer-first
rules (answer_first.py). An ordinary leg failure returns
`{"error": ..., "results": []}` under its section and the others still
answer. Typed input refusals propagate instead of producing partial results.
The MCP `loom_search` tool renders this payload as its text and lean result;
the `search` operation returns it as is.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, Literal

from server.core import request_tail
from server.core.ch import set_query_tag
from server.core.diagnostics import private_work
from server.core.errors import LoomServerError, VectorInputTooLargeError, caller_text
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import ReadFilters
from server.knowledge.app import repo_docs, source_publication
from server.knowledge.app.source_access import ReadPlan, prepare
from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app.agent_rules import request_rule_agent, served_hits
from server.memory.app.ask.reader_render import render_reader_block
from server.memory.app.loom_search.answer_first import answer_first
from server.memory.app.loom_search.repo_decisions import memory_with_repo_decisions
from server.memory.app.loom_search.tracker_leg import TrackerSearchPlan, tracker_leg
from server.memory.app.profile_search import search_profile
from server.memory.domain import code_change

if TYPE_CHECKING:
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput, KnowledgeSearchOutput

log = logging.getLogger(__name__)

# Query words that name nothing in a profile section — otherwise "where does
# the user live" matches every persona that says "user".
_PROFILE_STOPWORDS: frozenset[str] = frozenset(
    {
        "about",
        "after",
        "again",
        "before",
        "being",
        "could",
        "does",
        "doing",
        "during",
        "every",
        "first",
        "from",
        "have",
        "having",
        "here",
        "into",
        "just",
        "know",
        "like",
        "more",
        "most",
        "much",
        "only",
        "other",
        "over",
        "please",
        "same",
        "should",
        "since",
        "some",
        "still",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "thing",
        "things",
        "this",
        "those",
        "through",
        "under",
        "user",
        "users",
        "very",
        "want",
        "what",
        "when",
        "where",
        "which",
        "while",
        "with",
        "would",
        "your",
    }
)


def profile_hit_matches(query: str, label: str, value: str) -> bool:
    """A profile section matches when the whole query, or a query word of
    three or more letters, appears in its label — or when the whole query,
    or a query word of five or more letters that is not a stopword, appears
    in its value. Labels are names ("persona", "home"); values are prose,
    where short words match everything."""
    ql = str(query or "").lower().strip()
    if not ql:
        return False
    words = [t.strip(".,;:!?\"'()") for t in ql.split()]
    label_l = str(label or "").lower()
    if ql in label_l or any(len(t) > 2 and t in label_l for t in words):
        return True
    value_l = str(value or "").lower()
    if not value_l:
        return False
    if ql in value_l:
        return True
    return any(len(t) >= 5 and t not in _PROFILE_STOPWORDS and t in value_l for t in words)


def knowledge_above_floor(
    knowledge: dict[str, Any], min_similarity: float | None = None
) -> dict[str, Any]:
    """The knowledge section with hits below `min_similarity` (default
    `mcp_loom_search_knowledge_min_similarity`) dropped; a hit with no
    similarity field stays."""
    if min_similarity is None:
        from server.settings import settings

        min_similarity = float(settings.mcp_loom_search_knowledge_min_similarity)
    hits = knowledge.get("results")
    if not isinstance(hits, list) or min_similarity <= 0:
        return knowledge
    kept = [
        h
        for h in hits
        if not isinstance(h, dict)
        or h.get("similarity") is None
        or float(h.get("similarity") or 0.0) >= min_similarity
    ]
    return {**knowledge, "results": kept}


_FloorMode = Literal["readmit", "ids_only", "drop"]


def floor_mode() -> _FloorMode:
    """loom_search's sub-floor handling (`mcp_loom_search_floor_mode`), validated."""
    from server.settings import settings

    mode = str(settings.mcp_loom_search_floor_mode)
    if mode == "readmit":
        return "readmit"
    if mode == "ids_only":
        return "ids_only"
    return "drop"


def drop_flagged_hits(memory: dict[str, Any]) -> dict[str, Any]:
    """The memory section without the rows the calibration flagged `below_threshold`,
    when at least one row is not flagged. The floor (`floor_mode`) and the abstain
    calibration are two thresholds: `drop` removes rows under the similarity floor,
    while the calibration flags rows under its own, higher bar — the reader is told to
    ignore those, so the agent surface does not serve them. The reader block loses their
    paragraphs when `search_sections` renders the block from the final rows. When
    every row is flagged the section is left as is: an abstain-shaped answer keeps its
    evidence."""
    hits = memory.get("results")
    if not isinstance(hits, list) or not hits:
        return memory
    flagged = [h for h in hits if isinstance(h, dict) and h.get("below_threshold")]
    if not flagged or len(flagged) == len(hits):
        return memory
    kept = [h for h in hits if not (isinstance(h, dict) and h.get("below_threshold"))]
    return {**memory, "results": kept, "below_threshold_dropped": len(flagged)}


async def _conversation_leg(conv_input: Any, scope: ReadFilters) -> Any:
    """The conversation leg. Its rows carry no user bound the leg could apply,
    so a read scoped below the namespace gets no conversations."""
    from server.knowledge.api.models_conversations import ConversationSearchOutput
    from server.knowledge.app.conversations import handle_conversation_search

    if scope.narrowed:
        return ConversationSearchOutput(hits=[])
    return await handle_conversation_search(conv_input)


async def _prepare_knowledge(
    input: KnowledgeSearchInput, repo_namespace: str
) -> tuple[repo_docs.RepoSearchRequest | Exception, ReadPlan]:
    try:
        request = await repo_docs.resolve_repo_search(input, repo_namespace=repo_namespace)
        return request, await prepare(input.org, request.namespaces)
    except Exception as error:  # noqa: BLE001 — a failed knowledge scope leaves other sections available
        return error, ReadPlan(input.org)


async def _run_knowledge(
    prepared: repo_docs.RepoSearchRequest | Exception, plan: ReadPlan
) -> repo_docs.RepoSearch:
    if isinstance(prepared, Exception):
        raise prepared
    return await repo_docs.run_repo_search(prepared, plan)


async def _filtered_knowledge(
    first: KnowledgeSearchOutput,
    widen: Callable[[int], Awaitable[KnowledgeSearchOutput]],
    agent: str,
    top_k: int,
) -> dict[str, Any]:
    know = first.model_dump()

    async def expanded(k: int) -> list[dict[str, Any]]:
        wider = await widen(k)
        # Widening changes the served reranker; other metadata belongs to the first pass.
        know["reranked_by"] = wider.reranked_by
        return list(wider.model_dump()["results"])

    know["results"] = await served_hits(
        know["results"], expanded, lambda hit: hit["tags"], request_rule_agent(agent), top_k
    )
    return knowledge_above_floor(know)


async def _ordinary_knowledge(
    first: repo_docs.RepoSearch,
    wider: repo_docs.RepoSearch | None,
    agent: str,
) -> dict[str, Any]:
    await first.ordinary()

    async def expanded(k: int) -> KnowledgeSearchOutput:
        if wider is not None:
            # A wider fallback is additive; served_hits keeps the first on failure.
            await wider.ordinary()
            return wider.output
        request = first.request.wider(k)
        # The saved authorized scopes run without any new mirror discovery.
        ordinary = await repo_docs.run_repo_search(request, ReadPlan(request.base.input.org))
        return await ordinary.current()

    return await _filtered_knowledge(
        first.output, expanded, agent, int(first.request.base.input.top_k)
    )


async def _knowledge_section(
    first: repo_docs.RepoSearch, plan: ReadPlan, agent: str
) -> dict[str, Any]:
    wider: repo_docs.RepoSearch | None = None
    widening_error: Exception | None = None
    top_k = int(first.request.base.input.top_k)

    async def expanded(k: int) -> KnowledgeSearchOutput:
        nonlocal wider, widening_error
        try:
            wider = await repo_docs.run_repo_search(first.request.wider(k), plan)
        except Exception as error:
            widening_error = error
            raise
        return wider.output

    if not plan.marked:
        return await _filtered_knowledge(first.output, expanded, agent, top_k)
    set_query_tag("knowledge.mirror.fanout")
    await first.current()
    if not first.fresh():
        return await _ordinary_knowledge(first, None, agent)
    initial = await _filtered_knowledge(first.output, expanded, agent, top_k)
    if wider is None and widening_error is None:
        if first.fresh():
            for proof in first.publication_proofs:
                source_publication.retain(proof)
            return initial
        return await _ordinary_knowledge(first, None, agent)
    await first.current()
    if not first.fresh():
        return await _ordinary_knowledge(first, wider, agent)
    used_wider = False

    async def cached(_k: int) -> KnowledgeSearchOutput:
        nonlocal used_wider
        used_wider = True
        if widening_error is not None:
            raise widening_error
        assert wider is not None
        # Renew only when the renewed first pass still needs the wider result.
        await wider.current()
        return wider.output

    final = await _filtered_knowledge(first.output, cached, agent, top_k)
    if not first.fresh() or (used_wider and wider is not None and not wider.fresh()):
        return await _ordinary_knowledge(first, wider, agent)
    for result in (first, wider) if used_wider and wider is not None else (first,):
        for proof in result.publication_proofs:
            source_publication.retain(proof)
    return final


async def _published_knowledge(
    first: repo_docs.RepoSearch, plan: ReadPlan, agent: str
) -> dict[str, Any]:
    with request_tail.publication_group() as publication:
        section = await _knowledge_section(first, plan, agent)
        # answer_first retains section metadata and counts withheld hits.
        publication.commit()
        return section


async def search_sections(
    *,
    org: str,
    namespace: str,
    agent: str,
    query: str,
    top_k: int,
    tags: list[str],
    namespace_scope: Literal["this", "all_mine"],
    recipe: Literal["", "fast", "balanced", "quality", "verbatim", "code", "temporal", "graph"],
    bodies: Literal["full", "excerpt", "none", "unit"],
    page: str,
    explain: bool,
    repo_namespace: str,
    tracker_plan: TrackerSearchPlan | None,
) -> dict[str, Any]:
    """The assembled loom_search payload for `query` addressed to
    (`org`, `namespace`); every leg reads the namespaces `namespaces_to_search`
    gives for `namespace_scope`, and the knowledge leg leaves out the rules
    promoted for a bot other than the one `request_rule_agent(agent)` names. The
    caller has already authorized `namespace`; `repo_namespace` passed the
    repository gate (`repo_namespace_in_scope`; "" for none)."""
    from server.auth.fanout import namespaces_to_search
    from server.knowledge.api.models_conversations import ConversationSearchInput
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput

    scope = request_scope()
    namespaces, skipped = await namespaces_to_search(org, namespace, namespace_scope)

    mem_input = MemorySearchInput(
        org=org,
        namespace=namespace,
        # Cross-agent: searches span what every tool wrote (Loom's shared-
        # across-tools value prop); single-agent scoping would miss most hits.
        agent="*",
        query=query,
        top_k=top_k,
        tags=tags,
        namespace_scope=namespace_scope,
        recipe=recipe,
        bodies=bodies,
        # The agent surface always gets the "there is more" affordance.
        paging=True,
        page=page,
        floor_mode=floor_mode(),
        quality_filter=True,
        # The memory section is the reader block memory.ask reads (dated, in ask
        # order, whole excerpts). It is rebuilt below from the rows kept; this
        # search's render supplies the reader_instructions.
        render="reader",
    )
    know_input = KnowledgeSearchInput(
        org=org,
        namespace=namespace,
        query=query,
        top_k=top_k,
        tags=tags,
        namespace_scope=namespace_scope,
    )
    conv_input = ConversationSearchInput(
        org=org,
        namespace=namespace,
        agent="*",
        query=query,
        top_k=top_k,
        namespace_scope=namespace_scope,
    )
    prepared, plan = await _prepare_knowledge(know_input, repo_namespace)

    with private_work() if plan.marked else nullcontext():
        mem_result, know_result, profile_rows, conv_result, t_section = await asyncio.gather(
            # A later page continues the home search alone; the knowledge leg re-runs on
            # every page and keeps the repository's documents.
            memory_with_repo_decisions(mem_input, repo_namespace="" if page else repo_namespace),
            _run_knowledge(prepared, plan),
            search_profile(  # By words, then by meaning
                org, namespaces, query, top_k=top_k, lexical=profile_hit_matches, scope=scope
            ),
            _conversation_leg(conv_input, scope),
            tracker_leg(query, plan=tracker_plan, top_k=top_k),
            return_exceptions=True,
        )

        fatal = next(
            (
                result
                for result in (mem_result, know_result, profile_rows, conv_result, t_section)
                if isinstance(result, VectorInputTooLargeError)
                or (isinstance(result, BaseException) and not isinstance(result, Exception))
            ),
            None,
        )
        if fatal is not None:
            raise fatal

        # The QA verdict leads when the question is a defect report.
        payload: dict[str, Any] = {
            **({"tracker": t_section} if isinstance(t_section, dict) else {}),
            "namespaces_skipped": skipped,
        }
        # Each dumped row and its search model, by the row's id. Holding the row keeps its
        # id from being reused, so a step that copied a row would fail the lookup.
        models: dict[int, tuple[dict[str, Any], _MemorySearchHit]] = {}
        # The input the memory search ran (the block renders with its sanitized query,
        # as memory.search does); None for a parked page, which searched nothing.
        settled: MemorySearchInput | None = None
        moved: frozenset[int] = frozenset()  # id() of the memory dicts the search moved
        if isinstance(mem_result, Exception):
            payload["memory"] = {"error": _leg_error("memory", mem_result), "results": []}
        else:
            memory = mem_result.model_dump()
            models = {
                id(d): (d, m) for d, m in zip(memory["results"], mem_result.results, strict=True)
            }
            settled = mem_result._settled_input
            moved = frozenset(
                i for i, (_, m) in models.items() if code_change.moved_for_stale_code(m)
            )
            payload["memory"] = drop_flagged_hits(memory)
        if not isinstance(know_result, Exception):
            try:
                payload["knowledge"] = await _published_knowledge(know_result, plan, agent)
            except Exception as error:  # noqa: BLE001 — a failed knowledge renewal leaves other sections available
                know_result = error
        if isinstance(know_result, Exception):
            payload["knowledge"] = {"error": _leg_error("knowledge", know_result), "results": []}
        payload["profile"] = {
            "results": [] if isinstance(profile_rows, Exception) else list(profile_rows)
        }

        # Conversations: thread-history search, bundled so "have we discussed X"
        # lands in the same call.
        conv_hits: list[dict[str, Any]] = []
        if isinstance(conv_result, Exception):
            payload["conversations"] = {
                "error": _leg_error("conversations", conv_result),
                "results": [],
            }
        else:
            conv_dump = conv_result.model_dump()
            conv_hits = conv_dump.get("results") or conv_dump.get("hits") or []
            # One list, not the same hits twice under `hits` and `results`.
            conv_dump.pop("hits", None)
            payload["conversations"] = {**conv_dump, "results": conv_hits}

        payload = answer_first(payload, namespace, explain=explain, moved=moved)
        if settled is not None:
            # The block is rendered from the rows left after trimming, through their models:
            # the dumps hide the score_components the restatement notes read. The
            # repository's rows are labelled, unless the search is addressed to it.
            mem = payload["memory"]
            served = [models[id(hit)][1] for hit in mem["results"]]
            label = "" if repo_namespace == namespace else repo_namespace
            mem["reader_block"] = render_reader_block(served, settled.query, label_namespace=label)
        return payload


def _leg_error(leg: str, e: Exception) -> str:
    """A failed leg's error as the payload shows it: a Loom error leads with
    its code (`EMBED_TIMEOUT: ...`), so an agent can tell what to do. Any other
    error names its kind: its text can carry a statement or a server address,
    so it goes to the server log."""
    if not isinstance(e, LoomServerError):
        log.warning("loom_search: the %s leg failed: %s: %s", leg, type(e).__name__, e)
    return caller_text(e)
