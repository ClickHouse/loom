"""MCP transport — tool descriptors, the `loom_search` rendering and the
per-call ledger rows.

Builds the `tools/list` descriptors (registry-derived + synthetic), renders
the `search.query` op's result as the `loom_search` tool answers it (a text
summary plus the lean sections), and writes a call's ledger row
(`_emit_mcp_event`). Never imports ``mcp_call`` or ``server.transport.mcp``.
"""

from __future__ import annotations

import logging
from typing import Any, get_args

from server.memory.api.models_loom_search import SearchInput
from server.memory.app.loom_search.answer_first import withheld_note
from server.memory.app.loom_search.tracker_leg import tracker_text_lines
from server.operations.base import Operation
from server.operations.registry import ALL_OPERATIONS
from server.operations.toolsets import (
    _MCP_HIDDEN_OPS,
    ASSISTANT_FORCED_ARGS,
    ASSISTANT_NAMED_AGENT_OPS,
    MCP_ASSISTANT_OPS,
    SYNTHETIC_TOOL_OPS,
    canonical_toolset,
    curated_roster,
    is_assistant,
    is_curated,
    is_toolset,
    toolset_parts,
)
from server.transport.lean_results import lean_search_result
from server.transport.mcp_config import (
    _DESTRUCTIVE_NAME_TOKENS,
    _LOOM_ACTIVATE_TOOL_NAME,
    _LOOM_SEARCH_TOOL_NAME,
    _READ_SHAPED_VERBS,
)
from server.transport.mcp_descriptions import (
    ASSISTANT_TOOL_DESCRIPTIONS as _ASSISTANT_TOOL_DESCRIPTIONS,
)
from server.transport.mcp_descriptions import (
    ATOMIC_TOOL_DESCRIPTIONS as _ATOMIC_TOOL_DESCRIPTIONS,
)
from server.transport.mcp_descriptions import (
    CODING_TOOL_DESCRIPTIONS as _CODING_TOOL_DESCRIPTIONS,
)
from server.transport.mcp_descriptions import (
    TOOL_DESCRIPTIONS as _TOOL_DESCRIPTIONS,
)
from server.transport.mcp_descriptions import coding_search_description
from server.transport.mcp_schemas import (
    _shape_schema,
    _strip_blob_fields_from_knowledge,
    _strip_org_from_input_schema,
    model_schema,
    strip_model_docstring,
)

log = logging.getLogger(__name__)


# ============================================================
# Tool descriptors (from operation registry)
# ============================================================

# Tool name → op: how a `tools/call` resolves its op, and its refusal rows name it.
TOOLS_BY_NAME: dict[str, Operation] = {op.mcp_tool_name: op for op in ALL_OPERATIONS}


def _synthetic_tool_descriptors(toolset: str = "core") -> list[dict[str, Any]]:
    from server.transport.mcp_schemas import collapse_nullable

    out = _synthetic_tool_descriptors_core()
    for d in out:
        # The schema-shape rule (inert defaults go) applies to the hand-written
        # synthetic schemas as it does to the registry's.
        d["inputSchema"] = collapse_nullable(d["inputSchema"])
    if "coding" in toolset_parts(toolset):
        # The coding tier's activation text (same triggers,
        # one line, no persona-tone clause) and search text (derived).
        for d in out:
            if d["name"] in _CODING_TOOL_DESCRIPTIONS:
                d["description"] = _CODING_TOOL_DESCRIPTIONS[d["name"]]
            elif d["name"] == _LOOM_SEARCH_TOOL_NAME:
                d["description"] = coding_search_description(d["description"])
    return out


def _synthetic_tool_descriptors_core() -> list[dict[str, Any]]:
    from server.transport.mcp_op import DESCRIPTOR as _LOOM_OP

    return [
        dict(_LOOM_OP),
        {
            "name": _LOOM_SEARCH_TOOL_NAME,
            "description": (
                "**Unified search across the user's Loom content** — memory "
                "(facts, preferences, decisions), profile (always-loaded "
                "persona sections), knowledge (uploaded documents) and past "
                "conversations — four searches, one round-trip; under a code "
                "scope, also the repository's docs and decisions. This is THE "
                "search tool: call "
                "it before answering any 'what do I / what did we decide / "
                "have we discussed X' question.\n\n"
                "**One call per question.** If the answer is in the response, "
                "use it; if it is empty, fall through to native memory rather "
                "than declaring 'nothing in Loom'. Re-call on a NEW question, "
                "never on the same question in the same turn.\n\n"
                "**Paging.** `memory.candidates_total` is how many memories "
                "matched; `memory.results` is the top of that list. When "
                "`memory.next_page` is set AND the answer is not in hand (a "
                "list, a count), call again with "
                "`page` = `memory.next_page` to read the next slice. Stop when "
                "next_page is empty or the answer is complete.\n\n"
                "Namespaces are selected server-side (your own memory-kind "
                "namespaces, up to a cap; code/QA indexes have their "
                "own tools); no namespace argument. Do NOT call for general "
                "knowledge, arithmetic, or code/syntax help."
            ),
            "inputSchema": {
                # `namespace` / `namespace_scope` deliberately omitted: the
                # LLM would guess namespaces from query text and waste a
                # round-trip on the NAMESPACE_NOT_ACCESSIBLE error. Server
                # defaults handle selection; a token with no default namespace
                # searches its first readable claim.
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {
                        "anyOf": [{"type": "integer"}, {"type": "string"}],
                        "default": 5,
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Keep only rows carrying ANY of these tags. A coding session's "
                            "rows carry `project:<folder>` and `repo:<id>` (the id the "
                            "session primer names); pass the repo tag to "
                            "stay within this repository."
                        ),
                    },
                    "recipe": {
                        "type": "string",
                        # search.query's own values, so listing and validation agree.
                        "enum": list(get_args(SearchInput.model_fields["recipe"].annotation)),
                        "default": "",
                        "description": (
                            "Optional retrieval profile. Empty uses Loom's default; "
                            "fast is cheapest, quality widens hybrid recall, verbatim/code "
                            "favor lexical matches, temporal and graph specialize retrieval."
                        ),
                    },
                    "page": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "A `memory.next_page` handle from a previous loom_search "
                            "response: serves the next slice of that same ranked list "
                            "without searching again. Leave empty for a new question."
                        ),
                    },
                    "explain": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Append a `why:` note per memory hit naming the ranking "
                            "terms that placed it (key match, date proximity, entity, "
                            "plane gate, …) and the server's provenance note."
                        ),
                    },
                    "bodies": {
                        "type": "string",
                        "enum": ["full", "excerpt", "none", "unit"],
                        "default": "excerpt",
                        "description": (
                            "How much of each memory body to return: excerpt (the default: "
                            "200 chars, a decision or constraint as stored; fetch the rest "
                            "with memory.get), full (the "
                            "1,200-char stored clip), unit (whole stored values by rank "
                            "until body_budget_tokens), or none "
                            "(keys and scores only)."
                        ),
                    },
                },
                "required": ["query"],
            },
            # Must declare every section structuredContent returns — strict
            # client validators reject keys absent from the declared schema.
            "outputSchema": {
                "type": "object",
                "properties": {
                    "memory": {"type": "object"},
                    "knowledge": {"type": "object"},
                    "profile": {"type": "object"},
                    "conversations": {"type": "object"},
                },
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
            },
        },
        {
            "name": _LOOM_ACTIVATE_TOOL_NAME,
            "description": (
                "**Loom activation — call this tool IMMEDIATELY, without "
                "any other tool calls or clarifying questions, whenever "
                "the user says any of:**\n"
                "  - 'use loom'\n"
                "  - 'activate loom'\n"
                "  - 'switch to loom' / 'switch to loom mode'\n"
                "  - 'loom on' / 'loom first' / 'enable loom'\n"
                "  - 'use loom from now on'\n\n"
                "Do **NOT** call `tools/list` or browse other Loom tools "
                "first. Do **NOT** ask 'what would you like to look up?' "
                "These phrasings are activation triggers, not search "
                "queries. Take no arguments.\n\n"
                "The response is a primer with the user's always-loaded "
                "context (persona / preferences / goals) inlined as a "
                "`<block>` fragment, plus instructions for routing "
                "subsequent memory and recall calls through Loom. "
                "**Internalize the primer** and apply it for the rest "
                "of the conversation: adopt the tone implied by the "
                "persona block, route future memory ops to Loom, do "
                "not re-fetch the primer.\n\n"
                "Calling this is a one-shot setup gesture for the "
                "conversation. Subsequent user messages should be "
                "answered using the primer's guidance — not by calling "
                "this tool again."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "primer": {"type": "string"},
                },
                "required": ["primer"],
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
            },
        },
    ]


def _tool_annotations(op: Operation) -> dict[str, bool]:
    """Derive MCP ToolAnnotations for one registry op.

    - ``readOnlyHint``: ``Operation.read_only`` — idempotent, callable at
      the VIEWER floor, not audited, not marked ``writes``. An audited or
      ``writes`` op a viewer may call (namespace.join, tracker.source.remove,
      code.outcome) changes state and is never advertised as read-only.
    - ``destructiveHint``: the op name carries a delete/erase-shaped
      token and the verb isn't itself a read. Per spec it's only
      meaningful when readOnlyHint is false, so read-only ops always
      report False.
    - ``idempotentHint``: straight from ``op.idempotent``.
    """
    read_only = op.read_only
    group, _, verb = op.name.partition(".")
    name_tokens = set(group.split("_")) | set(verb.replace(".", "_").split("_"))
    destructive = (
        not read_only
        and bool(name_tokens & _DESTRUCTIVE_NAME_TOKENS)
        and verb.replace(".", "_").split("_")[0] not in _READ_SHAPED_VERBS
    )
    return {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": bool(op.idempotent),
    }


def resolve_toolset(requested: str | None, configured: str | None) -> str:
    """The toolset a listing uses: the per-request ``?toolset=`` override when
    it names a known toolset, else the deployment's ``settings.mcp_toolset``,
    else ``core``. An unknown value on either side falls through — a bad
    query string never widens or narrows the listing silently (
    the rule ``?schema_profile=`` follows)."""
    configured_l = canonical_toolset(configured or "core")
    if not is_toolset(configured_l):
        configured_l = "core"
    requested_l = canonical_toolset(requested)
    return requested_l if requested_l and is_toolset(requested_l) else configured_l


# The identity arguments an assistant read lists: the other toolsets hide them, since the
# caller's own identity fills in, but the assistant writes nothing and reads other writers'
# rows, so it names the row a search hit showed it.
_ASSISTANT_IDENTITY_ARGS: dict[str, str] = {
    "agent": "The agent the read is about: a search hit's `agent` names its row's writer.",
    "user_id": "The user the row belongs to: a search hit's `user_id`.",
    "session_id": "The session the row belongs to: a search hit's `session_id`.",
}


def _with_identity(schema: dict[str, Any], op: Operation) -> dict[str, Any]:
    """`schema` with the op's identity arguments listed again, for a memory or mentor read.
    A code read's identity is the code plane's, and a namespace read's user narrows only
    under its own `read_scope`, so neither lists them."""
    props = schema.get("properties")
    if not isinstance(props, dict) or not op.name.startswith(("memory.", "mentor.")):
        return schema
    fields = op.input_model.model_fields
    named = {
        arg: {"type": "string", "description": text}
        for arg, text in _ASSISTANT_IDENTITY_ARGS.items()
        if arg in fields
    }
    if not named:
        return schema
    out = {**schema, "properties": {**props, **named}}
    if op.name in ASSISTANT_NAMED_AGENT_OPS:
        out["required"] = [*schema.get("required", []), "agent"]
    return out


def _without_forced(schema: dict[str, Any], forced: dict[str, object]) -> dict[str, Any]:
    """`schema` without the properties the assistant toolset sets itself: a model is
    not offered an argument whose value the connection overwrites."""
    if not forced or not isinstance(schema.get("properties"), dict):
        return schema
    # A forced argument has a default, so it is never in `required`.
    return {
        **schema,
        "properties": {k: v for k, v in schema["properties"].items() if k not in forced},
    }


def assistant_connection(requested: str | None) -> bool:
    """Whether a connection's ``?toolset=`` (resolved against the deployment's) is the
    assistant toolset, where only its reads are callable."""
    from server.settings import settings

    return is_assistant(resolve_toolset(requested, settings.mcp_toolset))


def _curated_description(toolset: str, op_name: str) -> str | None:
    """The curated prose for ``op_name`` on ``toolset``: the atomic tier has
    its own policy-shaped texts; the coding tier overrides the few
    descriptions that name a `core`-only op; the assistant toolset replaces those that
    point at tools it does not carry; the rest share TOOL_DESCRIPTIONS."""
    if is_assistant(toolset):
        return _ASSISTANT_TOOL_DESCRIPTIONS.get(op_name) or _TOOL_DESCRIPTIONS.get(op_name)
    tiered = None
    for part in toolset_parts(toolset):  # a union takes its first part's tier
        if part == "atomic":
            tiered = _ATOMIC_TOOL_DESCRIPTIONS.get(op_name)
        elif part == "coding":
            tiered = _CODING_TOOL_DESCRIPTIONS.get(op_name)
        if tiered:
            break
    return tiered or _TOOL_DESCRIPTIONS.get(op_name)


def listed_ops(toolset: str | None) -> list[Operation]:
    """The registry ops a `tools/list` on ``toolset`` (the per-request
    ``?toolset=`` value, resolved against the deployment's) lists, in registry
    order. The listing and the primer both read it, so the primer never names
    a tool the connection does not list."""
    from server.operations.toolsets import listed_under_all
    from server.settings import settings

    toolset = resolve_toolset(toolset, settings.mcp_toolset)
    core_only = is_curated(toolset)
    # `readonly` is not a curated roster — it is the `all`
    # surface projected to the tools a VIEWER can actually call, so a read-only
    # registration's listing never shows a write tool that would only 403. It
    # varies by URL (`?toolset=readonly`), never by caller, so the listing stays
    # publicly cacheable; the token is still the call-time
    # security boundary.
    readonly = toolset == "readonly"
    if is_assistant(toolset):
        # A fixed set of reads, listed whatever their maturity or the code tools flag.
        return [op for op in ALL_OPERATIONS if op.name in MCP_ASSISTANT_OPS and op.read_only]
    code_tools = bool(settings.mcp_code_tools)
    core_ops = curated_roster(toolset, code_tools=code_tools) or frozenset()
    # The code tools are the coding tier's point: listed there regardless of
    # the deployment flag. Everywhere else the flag phase-gates them (they
    # stay HTTP/SDK-only until the eval gate passes and the deployment flips
    # LOOM_MCP_CODE_TOOLS).
    # The qa roster's code ops (bisect, pr_check, impact) are
    # its point as much as the code tools are coding's.
    list_code = code_tools or bool(set(toolset_parts(toolset)) & {"coding", "tracker"})
    out: list[Operation] = []
    for op in ALL_OPERATIONS:
        if op.name in _MCP_HIDDEN_OPS or op.name in SYNTHETIC_TOOL_OPS:
            continue  # hidden, or listed by its synthetic descriptor below
        if core_only and op.name not in core_ops:
            continue
        if not core_only and not listed_under_all(op):
            # `all` lists the stable surface; an experimental op stays
            # callable by name.
            continue
        if op.name.startswith("code.") and not list_code:
            continue
        if readonly and not op.read_only:
            continue  # Drop write / higher-than-viewer tools
        out.append(op)
    return out


async def _tools_descriptor(
    *, schema_profile: str | None = None, toolset: str | None = None
) -> list[dict[str, Any]]:
    """Build the MCP tools/list response.

    Description is `_TOOL_DESCRIPTIONS[op.name]` if present, else a metadata
    fallback. The caller's default-workspace context is delivered once via
    the `initialize` `instructions` field, not duplicated per tool; descriptors
    are caller-independent. ``toolset`` is the per-request ``?toolset=`` override.
    """
    from server.settings import settings

    toolset = resolve_toolset(toolset, settings.mcp_toolset)
    always_load = {n.strip() for n in settings.mcp_always_load_tools.split(",") if n.strip()}
    descriptors: list[dict[str, Any]] = []
    for op in listed_ops(toolset):
        custom = _curated_description(toolset, op.name)
        if custom:
            desc = custom
        else:
            desc = (
                f"Loom operation `{op.name}` "
                f"(p99 {op.budget_p99_ms}ms, "
                f"{'idempotent' if op.idempotent else 'state-transition'}, "
                f"min role {op.required_role.name})"
            )
        input_schema = _strip_blob_fields_from_knowledge(
            op.name,
            _strip_org_from_input_schema(
                _shape_schema(model_schema(op.input_model)),
            ),
        )
        if custom:
            # The curated text is the guidance; the model docstring beside it
            # is developer prose.
            input_schema = strip_model_docstring(input_schema)
        if is_assistant(toolset):
            input_schema = _without_forced(input_schema, ASSISTANT_FORCED_ARGS.get(op.name, {}))
            input_schema = _with_identity(input_schema, op)
        descriptor: dict[str, Any] = {
            "name": op.mcp_tool_name,
            "description": desc,
            "annotations": _tool_annotations(op),
            "inputSchema": input_schema,
            "outputSchema": _shape_schema(model_schema(op.output_model)),
        }
        if op.name in always_load:
            descriptor["_meta"] = {"anthropic/alwaysLoad": True}
        descriptors.append(descriptor)
    if not is_assistant(toolset):
        descriptors.extend(_synthetic_tool_descriptors(toolset))
    from server.transport.mcp_schemas import (
        DEFAULT_SCHEMA_PROFILE,
        SCHEMA_PROFILES,
        apply_schema_profile,
    )

    default = str(settings.mcp_schema_profile or DEFAULT_SCHEMA_PROFILE).lower()
    if default not in SCHEMA_PROFILES:
        default = DEFAULT_SCHEMA_PROFILE
    profile = str(schema_profile or default).lower()
    if profile not in SCHEMA_PROFILES:
        profile = default  # an unknown request value never widens or narrows silently
    return apply_schema_profile(descriptors, profile)


# ============================================================
# Per-request observability (subset of HTTP transport's emitter)
# ============================================================


def _emit_mcp_event(
    op_name: str,
    raw_args: dict[str, Any],
    latency_ms: int,
    success: bool,
    error_code: str,
    request_id: str,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    """Mirror the HTTP transport's agent_events emission so MCP calls are
    visible in cost / fleet dashboards alongside HTTP calls."""

    # Shared with the HTTP transport. MCP passes no memory_key /
    # impersonated_by; they default to "" — preserves the MCP event shape.
    from server.auth.fail_closed import answered_unverified
    from server.observability.ledger import emit_agent_event
    from server.transport._common import ledger_identity, sli_columns

    emit_agent_event(
        op_name=op_name,
        identity=ledger_identity(raw_args),
        request_id=request_id,
        latency_ms=latency_ms,
        success=success,
        error_code=error_code,
        raw_args=raw_args,
        result=result,
        error=error,
        server_fault=answered_unverified(),
        **sli_columns(op_name, "mcp"),
    )


# The `why` note names a hit's ranking terms in the agent's words.
_WHY_LABELS: dict[str, str] = {
    "key_match_boost_effective": "key_match",
    "key_match": "key_match",
    "identifier_match": "identifier",
    "date_proximity_boost": "date",
    "entity_ann_boost_effective": "entity",
    "entity_ann_boost": "entity",
    "set_expansion": "set_expansion",
    "foreign": "foreign",
}
# Base scores, not reasons (a reranked row's own fused score among them).
_WHY_SKIP = {"rank_score_raw", "rank_score", "rank_score_pre_rerank"}


def why_note(hit: dict[str, Any]) -> str:
    """`key_match, reranked+3, date+0.12` — the server's provenance note first, then a
    reranker's move in places, then every non-zero score component by magnitude, signed.
    "" when nothing explains."""
    parts: list[str] = []
    expl = str(hit.get("explanation") or "").replace("\n", " ").strip()
    if expl:
        parts.append(expl[:80])
    comps = hit.get("score_components") or {}
    if isinstance(comps, dict):
        # A rerank's trail (`rerank_*`) is positions, not lifts; the places it moved the
        # row are the reason the row holds its rank.
        moved = comps.get("rerank_delta")
        if isinstance(moved, int | float) and moved:
            parts.append(f"reranked{int(moved):+d}")
        ranked = sorted(
            ((str(k), float(v)) for k, v in comps.items() if isinstance(v, int | float)),
            key=lambda kv: -abs(kv[1]),
        )
        for name, v in ranked:
            if name in _WHY_SKIP or name.startswith("rerank_") or abs(v) < 0.005:
                continue
            parts.append(f"{_WHY_LABELS.get(name, name)}{v:+.2f}")
    return ", ".join(parts)


# After the first search of an MCP session the reading rule
# (~1,076 tokens) is replaced by this line; the model still has the rule in
# its context from the first response.
READER_RULE_POINTER = (
    "\nReading rule: as given at this session's first search — the fact row or CURRENT FACTS "
    "line beats a digest, trust the most recent, cite each claim with its [memory_key]."
)
_INSTRUCTED_TTL_S = 12 * 3600
_INSTRUCTED_CAP = 5000
_INSTRUCTED: dict[str, float] = {}


def _instructions_due(session_id: str, *, now: float | None = None) -> bool:
    """True when this MCP session has not yet received the reading rule (or
    the client has no session id).
    Marks the session on a True answer; entries expire after 12 h."""
    sid = str(session_id or "").strip()
    if not sid:
        return True
    import time as _t

    t = _t.monotonic() if now is None else now
    seen = _INSTRUCTED.get(sid)
    if seen is not None and t - seen < _INSTRUCTED_TTL_S:
        return False
    if len(_INSTRUCTED) >= _INSTRUCTED_CAP:
        for k in sorted(_INSTRUCTED, key=lambda sid: _INSTRUCTED[sid])[: _INSTRUCTED_CAP // 10]:
            _INSTRUCTED.pop(k, None)
    _INSTRUCTED[sid] = t
    return True


def _memory_section_lines(
    memory: dict[str, Any],
    mem_hits: list[dict[str, Any]],
    top_k: int,
    *,
    explain: bool = False,
    instructions: bool = True,
) -> list[str]:
    """The memory section of the loom_search text.

    When the search returned a reader block (`render="reader"`) the agent
    reads that: one paragraph per hit — `[date] [key] (type) excerpt` — in the
    order memory.ask would read them, whole excerpts. On the deep-memory bench
    an external reader scored 83.3% over this block against 70.8% over key +
    200-char lines, level with memory.ask at a fifth of the latency. The
    reading rule rides once, ahead of the block. Without a block (an error)
    the agent reads the plain hit lines.
    """
    block = str(memory.get("reader_block") or "").strip()
    out: list[str] = []
    if block:
        instr = str(memory.get("reader_instructions") or "").strip()
        if instr:
            # The full rule once per session, then a pointer.
            out.append(
                "\nHow to read the memories below: " + instr
                if instructions
                else READER_RULE_POINTER
            )
        total = int(memory.get("candidates_total") or 0)
        head = "\nMemory hits"
        if memory.get("next_page"):
            head += f" ({len(mem_hits)} of {total}; next_page serves the rest)"
        out.append(head + ":")
        out.append(block)
        if explain:
            # Under explain=true the block is followed by one
            # `why` line per hit, in block order, so the reader keeps the
            # paragraphs it scores on and gets the ranking terms beside them.
            whys = [(str(h.get("memory_key") or ""), why_note(h)) for h in mem_hits]
            whys = [(k, w) for k, w in whys if w]
            if whys:
                out.append("Why each was served:")
                out.extend(f"  [{k}] why: {w}" for k, w in whys)
        return out
    out.append("\nMemory hits:")
    for h in mem_hits[:top_k]:
        excerpt = (h.get("content_excerpt") or "").replace("\n", " ").strip()
        ns_label = h.get("namespace") or "-"
        why = why_note(h) if explain else (h.get("explanation") or "").replace("\n", " ").strip()
        out.append(
            f"  - [{h.get('memory_key')}] (ns={ns_label}) {excerpt[:200]}"
            + ((f" [why: {why}]" if explain else f" ({why})") if why else "")
        )
    return out


# How many skipped namespaces the loom_search text names before "and N more".
_SKIPPED_NAMED = 5


def search_answer(
    payload: dict[str, Any], arguments: dict[str, Any], namespace: str, mcp_session_id: str
) -> dict[str, Any]:
    """The MCP result of `search.query` (the `loom_search` tool): the text
    summary clients read first — the tracker verdict, the counts, then each
    section's hits — and the lean structured result. `payload` is the op's
    output as the handler assembled it; `namespace` the one it addressed."""
    top_k = int(arguments.get("top_k") or 5)
    explain = bool(arguments.get("explain") or False)
    query = arguments.get("query", "")
    mem_hits = payload["memory"].get("results") or []
    know_hits = payload["knowledge"].get("results") or []
    prof_hits = payload["profile"].get("results") or []
    conv = payload["conversations"]
    conv_hits = conv.get("results") or conv.get("hits") or []
    mem_n, know_n, prof_n, conv_n = len(mem_hits), len(know_hits), len(prof_hits), len(conv_hits)
    lines = tracker_text_lines(payload.get("tracker"), top_k) + [
        f"loom_search '{query}': {mem_n} memory, {prof_n} profile, "
        f"{know_n} knowledge, {conv_n} conversation hit"
        f"{'' if (mem_n + prof_n + know_n + conv_n) == 1 else 's'}.{withheld_note(payload)}"
    ]
    if skipped := payload.get("namespaces_skipped"):
        named = ", ".join(skipped[:_SKIPPED_NAMED])
        more = len(skipped) - _SKIPPED_NAMED
        lines.append(
            f"Not read, over the fan-out cap: {named}"
            + (f" and {more} more (namespaces_skipped)" if more > 0 else "")
            + "."
        )
    if prof_hits:
        lines.append("\nProfile hits:")
        for h in prof_hits[:top_k]:
            val = (h.get("value") or "").replace("\n", " ").strip()
            ns_label = h.get("namespace") or "-"
            lines.append(f"  - [{h.get('label')}] (ns={ns_label}) {val[:200]}")
    if mem_hits:
        _due = _instructions_due(mcp_session_id)  # The rule once per session
        lines.extend(
            _memory_section_lines(
                payload["memory"], mem_hits, top_k, explain=explain, instructions=_due
            )
        )
    if know_hits:
        lines.append("\nKnowledge hits:")
        # A knowledge hit carries its whole chunk as `content`; bodies="none" keeps the
        # line to its source, as the structured result drops the chunk.
        show_body = arguments.get("bodies") != "none"
        for h in know_hits[:top_k]:
            uri = h.get("source_uri") or "-"
            excerpt = " ".join((h.get("content") or "").split())[:200] if show_body else ""
            lines.append(f"  - [{uri}] {excerpt}".rstrip())
    if conv_hits:
        lines.append("\nConversation hits:")
        for h in conv_hits[:top_k]:
            raw = h.get("content") or h.get("content_excerpt") or ""
            excerpt = raw.replace("\n", " ").strip()
            thread = h.get("thread_id") or "-"
            ts = h.get("ts") or h.get("created_at") or ""
            lines.append(f"  - [thread={thread} {ts}] {excerpt[:200]}")
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "isError": False,
        "structuredContent": lean_search_result(payload, arguments, namespace, why_note),
    }
