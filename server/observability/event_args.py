"""Project a request's arguments onto the `agent_events.args` map.

`agent_events` records WHO called WHAT and WHEN — `(org, namespace, agent,
session_id, operation, ts)` — and, of the call's arguments, only `memory_key`
in a column of its own. That is enough for cost and latency, not enough for a
history-aware question
("the same document", "the same memory", "a delete after a get of it") or
for replaying a run.

This module is the single place that decides which argument fields an event
keeps. Three classes, by field NAME, independent of operation:

* **identifiers** — copied verbatim, capped at `_MAX_IDENT_CHARS`. Keys,
  ids, types, paths, symbol ids, small numeric knobs. These are the fields a
  later predicate correlates on (`args['memory_key'] = ...`).
* **free text** — never stored; replaced by `sha256:<16 hex>/<length>` so
  "the same query" or "the same value" is answerable (and exact repeats
  countable) without persisting the text. `value`, `query`, `content`, …
* everything else — dropped. Identity fields are already columns; nested
  structures are neither identifiers nor text we can digest meaningfully.

Every event also carries `_sha256`: a digest of the whole argument object
with the identity fields removed, so an exact repeat of a call is one
equality on the map.

Pure: no I/O, never raises on odd input (a projection failure must not drop
the event). Bounded: at most `_MAX_ENTRIES` keys, each value bounded.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from server.auth.directory import DirectoryNotWired, directory
from server.operations.toolsets import ASSISTANT_FOUND_FIELD

# Fields that already live in dedicated columns on the row; never duplicated
# into the map.
IDENTITY_KEYS: frozenset[str] = frozenset(
    {"org", "namespace", "agent", "user_id", "session_id", "run_id"}
)

# Copied verbatim (bounded). Names, not values — `pattern` is an
# operator-authored regex, `path` a repo-relative file path, `name` a
# schema or procedure name; none is conversation text.
IDENTIFIER_KEYS: frozenset[str] = frozenset(
    {
        "memory_key",
        # The dashboard's paint telemetry (ui.vitals) — a route
        # id, milliseconds, counts and a build hash; never text.
        "route",
        "lcp_ms",
        "long_task_ms",
        "inp_ms",
        "dom_nodes",
        # insights.query: how many rows the statement returned, and the
        # statement's digest, which a restricted namespace keeps when it drops
        # the `sql` text key; the assistant's citation check reads it.
        "row_count",
        "sql_digest",
        "api_calls",
        "api_ms",
        "build",
        "cold",
        # The mentor's hint code on a response (`out_hint_code`)
        # so adoption — a hinted op called later in the session — is a query.
        "hint_code",
        "memory_id",
        "memory_type",
        "target_key",
        "block_key",
        "thread_id",
        "message_id",
        "symbol_id",
        "symbol",
        "name",
        "path",
        "prefix",
        "pattern",
        "source_uri",
        "source",
        "subject",
        "entity",
        "category",
        "label",
        "status",
        "decision",
        "outcome",
        "kind",
        "scope",
        "search_mode",
        "as_of",
        "as_of_basis",
        "from_org",
        "from_namespace",
        "consumer",
        "traffic_class",
        "role",
        "relation",
        "top_k",
        "limit",
        "token_budget",
        "depth",
        # memory.ask per-request pins of two deployment defaults: the
        # answer-length default and the routing prior's
        # threshold a call asked for, so an A/B's arms separate in the ledger
        # (`consistency`, the judge-mode pin, is already projected).
        "default_answer_chars",
        "route_prior_max_cheap",
        # memory.ask mid-tier pins: which model and routes a
        # call asked for, so the arms of an A/B separate in the ledger.
        "mid_model",
        "mid_routes",
        "offset",
        "char_limit",
        "ttl_seconds",
        "weight",
        "confidence",
        "pinned",
        "rerank",
        "classify",
        "observation_date",
        "valid_at",
        "tags",
        "memory_ids",
        "locations",
        # result-side identifiers (project_result): what a call returned
        "version",
        # The repair CLI's ledger row carries its counts
        # (`scanned` is already listed with the expiry sweep's)
        "written",
        "repaired",
        "raw_string",
        "double_encoded",
        "unclassified",
        "content_sha256",
        "duplicate_of",
        # What the write-time check decided and whether the
        # call wrote anything — `out_conflict_verdict='merge'` +
        # `out_noop='true'` is the noop row; dry_run is the argument.
        "conflict_verdict",
        "noop",
        # Whether an outcome label landed on a row (`memory.outcome` answers false for a
        # row it did not find), so a label counts only when it did.
        "recorded",
        # The write's advice to the agent about its VALUE
        # (today "value is base64-encoded (…)"), so the share of agents that
        # encode is one GROUP BY on the prefix.
        "advisories",
        "tombstoned",
        "removed",
        "run_id_created",
        "trace_run_id",
        # memory.ask routing outcome: which tier and
        # reading depth answered, so the fire rates are one GROUP BY away.
        "route_tier",
        "route_reason",
        "escalated",
        "deep_fired",
        "deep_reason",
        "deep_first",
        # The exact-prompt response cache's hit and
        # miss counts for the call (memory.ask's synthesis family; the
        # extractor's calls on set_from_messages), and the sha256 keys of the
        # rendered extractor prompts (no text) — the repeat share offline.
        "llm_cache_hits",
        "llm_cache_misses",
        "extract_prompt_keys",
        # memory.ask prompt: the synthesis-call count and the answering
        # model's prompt-cache counters and dollars, so the cached share on asks
        # with two or more synthesis calls and the strong-tier $/ask are one
        # GROUP BY away.
        "synth_calls",
        "synth_input_tokens",
        "synth_cached_input_tokens",
        "synth_cache_write_tokens",
        "synth_cost_usd",
        # memory.ask exact-prompt response cache: the
        # request pin (`llm_cache`, args side) and, on the result, whether the
        # cache wrapped the ask at all (`out_llm_cache` — the pin, the class
        # policy or the deployment default, whichever applied) and the dollars
        # a hit did not spend. The hit / miss counts (`llm_cache_hits`,
        # `llm_cache_misses`) are projected beside `deep_first` above, so
        # hits and misses are readable per ask.
        "llm_cache",
        "llm_cache_saved_usd",
        # memory.ask facet completion: rows the key-facet
        # sweep added, so the step's fire rate on list questions is measurable.
        "facet_memories_added",
        # memory.outline: size of the user's profile block.
        "profile_chars",
        # Identifier chase hops and rows on memory.ask.
        "identifier_chase_hops",
        "identifier_chase_added",
        # memory.link stored `related_to` for an empty relation.
        "relation_defaulted",
        # The trajectory planes report their outcomes: what an
        # observation holds, whether procedure.match hit, whether an ask
        # rejected its premise — each one GROUP BY away.
        "tool_name",
        "has_error",
        "narrative_chars",
        # Why observe.record stored a row content-less.
        "skipped_reason",
        "matched",
        "procedure_key",
        "premise_conflict",
        # memory.ask premise line: the synthesizer's verdict.
        "premise_verdict",
        # memory.ask attribution note: fired or not.
        "attribution_note",
        # memory.ask answer-vs-citation verdict: the label
        # and D-SMART score are identifiers of the answer's standing, not text.
        "consistency",
        "consistency_score",
        "deep_chars",
        "code_hits",
        # memory.ask synthesis-pool composition.
        "pool_facts",
        "pool_chunks",
        # memory.ask prompt block accounting: one map,
        # rendered `block=tokens,…` largest first (`extractKeyValuePairs`
        # reads it back), so the per-block share of the strong prompt is one
        # GROUP BY away without a key per block.
        "prompt_blocks",
        "context_tokens_est",
        "context_trimmed",
        # memory.search / memory.ask retrieval outcome: the
        # wild abstain rate, its code, the fallback legs that fired and the
        # calibrated risk, so one GROUP BY on agent_events answers "how often
        # does search abstain here".
        "abstain",
        "abstain_code",
        "fallback_rounds_used",
        "retrieval_risk",
        "retrieval_confidence",
        "route_agreement",
        "summary_hits",
        "top_k_ceiling",
        # Whether the read's recall credit was written
        # (`credited` | `not_credited (<reason>)`); absent on evaluation reads.
        "reinforcement",
        # What the caller could have paged to, and whether it did.
        "candidates_total",
        # Share of the returned window that is verbatim chunks
        "chunk_share",
        "page_offset",
        # scope and mode knobs: how wide a read was. Without
        # them a row says WHAT was asked but not how wide the read was, so
        # "which agent reads namespace-wide with top_k 30" is unanswerable.
        "read_scope",
        "read_user_id",
        "namespace_scope",
        "personalization",
        "decompose",
        "world_knowledge",
        "allow_synthesis",
        # memory.apply / memory.lock / memory.set lifecycle knobs
        "action",
        "mode",
        "on_expire",
        "dry_run",
        "revive",
        "include_expired",
        # result-side: how the answer was produced (abstain / abstain_code are
        # codes; abstain_reason is free text and stays off the row)
        "grounding",
        "decomposed_parts",
        "pending_evidence",
        "pending_extraction",
        # What the fail-open legs swallowed / the pool width
        "decompose_failed",
        "pool_k",
        # Whether the write's valid_at came from the caller or
        # the request time, so undated rows have a per-agent cause
        "observation_date_source",
        # Facts written as time-bounded foresight per extraction
        "foresight",
        # Memories retired on the user's request to forget
        "forgotten",
        # Requests to forget read from the user's sentences
        "forget_requests",
        # Rows written as another person's, from the sentence's subject
        "about_other",
        # The user's facts read from the assistant's premises
        "premise_facts",
        # How set_from_messages cut the session and into how
        # many extraction calls — the segmentation fire rate on the ledger
        "segments",
        "segmenter",
        # expiry sweep counts (worker rows, kind='worker')
        "scanned",
        "demoted",
        "anonymized",
        "locked_skipped",
        "moved_skipped",
        "failed",
        "timeline_rows",
        "count_recomputed",
        # Which procedures a context.pack put in front of the
        # agent — the shown side of a ledger-derived utility.
        "procedure_keys",
        "procedure_keys_withheld",
    }
)

# Digested, never stored. Conversation content, questions, rationale.
TEXT_KEYS: frozenset[str] = frozenset(
    {
        "value",
        "content",
        "text",
        "query",
        # insights.query's statement: its digest and length, never the text.
        "sql",
        "question",
        "description",
        "reason",
        "rationale",
        "reasoning",
        "title",
        "situation",
        "messages",
        "goal",
        "user_request",
        "final_response",
        "summary",
        "notes",
        "instructions",
        "body",
        "diff",
        "code",
    }
)

_MAX_IDENT_CHARS = 200
_MAX_LIST_ITEMS = 20
# A numeric map (`prompt_blocks`: up to 17 named blocks with
# four- or five-digit values) renders as `name=value` pairs, largest first,
# under its own bound: at 200 the smallest blocks fell off the row.
_MAX_MAP_CHARS = 400
_MAX_ENTRIES = 32
# The result side has its own, larger budget: memory.ask's output carries 35
# identifier keys, and a shared cap of 32 would silently drop the
# alphabetically-late `source`, `summary_hits` and `timeline_rows` — `source`
# being the key every ask analysis filters on. tests/unit/test_event_args.py
# pins that every identifier key of MemoryAskOutput fits.
_MAX_RESULT_ENTRIES = 64


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _canonical(value: Any) -> str:
    """Deterministic text for a value of any shape (dicts key-sorted)."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001 — never let a weird value drop the event
        return repr(value)


def _clip(text: str) -> str:
    if len(text) <= _MAX_IDENT_CHARS:
        return text
    return text[: _MAX_IDENT_CHARS - 1] + "…"


def _identifier(value: Any) -> str | None:
    """Render an identifier-class value. Scalars verbatim, short lists joined,
    anything else is not an identifier and is skipped."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return _clip(str(value))
    if isinstance(value, (list, tuple)):
        items = [
            str(v) for v in list(value)[:_MAX_LIST_ITEMS] if isinstance(v, (str, int, float, bool))
        ]
        if not items:
            return None
        more = len(value) - len(items)
        joined = ",".join(items) + (f",+{more}" if more > 0 else "")
        return _clip(joined)
    if isinstance(value, dict):
        return _numeric_map(value)
    # dates and other objects with a stable str
    if hasattr(value, "isoformat"):
        try:
            return _clip(value.isoformat())
        except Exception:  # noqa: BLE001 — a projection failure never drops the event
            return None
    return None


def _numeric_map(value: dict[Any, Any]) -> str | None:
    """A map of numbers as `k=v,k=v`, largest value first
    then by name — the `out_stage_ms` shape, which ClickHouse reads back
    with `extractKeyValuePairs(x, '=', ',')`. A map with any non-numeric
    value, or an empty one, is not an identifier and is skipped."""
    items: list[tuple[str, int | float]] = []
    for k, v in value.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        items.append((str(k), v))
    if not items:
        return None
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    text = ",".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in items)
    if len(text) <= _MAX_MAP_CHARS:
        return text
    return text[: _MAX_MAP_CHARS - 1] + "…"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    payload = _canonical(value)
    if payload == "":
        return None
    return f"sha256:{_digest(payload)}/{len(payload)}"


def text_digest(value: Any) -> str:
    """The ledger's `sha256:<hex>/<length>` for a text-class argument, '' for none."""
    return _text(value) or ""


def redact_args(raw_args: dict[str, Any] | None, org: str, namespace: str) -> dict[str, Any] | None:
    """The request arguments with every text-class value removed when the
    addressed namespace is restricted; unchanged otherwise. Identifiers
    (memory_key, path, pattern, …) stay: they are names, not content, and
    the ledger needs them to attribute the read. The check is the
    directory's cached one (no I/O); a process with no directory wired
    cannot tell, and redacts."""
    if not raw_args or not org or not namespace:
        return raw_args
    try:
        restricted = directory().is_restricted_cached(str(org), str(namespace))
    except DirectoryNotWired:
        restricted = True
    if not restricted:
        return raw_args
    out = {k: v for k, v in raw_args.items() if k not in TEXT_KEYS}
    out["_restricted"] = 1
    return out


def project_args(raw: Any) -> dict[str, str]:
    """Return the bounded `args` map for one request's arguments.

    Accepts anything (a dict body, a pydantic model already dumped, or
    garbage) and always returns a plain `dict[str, str]`.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    out: dict[str, str] = {}
    try:
        stripped = {k: v for k, v in raw.items() if k not in IDENTITY_KEYS}
        if stripped:
            out["_sha256"] = _digest(_canonical(stripped))
        for key in sorted(stripped):
            if len(out) >= _MAX_ENTRIES:
                break
            value = stripped[key]
            rendered: str | None
            if key in IDENTIFIER_KEYS:
                rendered = _identifier(value)
            elif key in TEXT_KEYS:
                rendered = _text(value)
            else:
                continue
            if rendered is not None:
                out[str(key)] = rendered
    except Exception:  # noqa: BLE001 — projection must never drop the event
        return out
    return out


RESULT_PREFIX = "out_"

_MAX_ERROR_CHARS = 200
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
# Exceptions whose quoted span is a Python identifier or dotted module path,
# never caller text: NameError("name 'x' is not defined"),
# UnboundLocalError("cannot access local variable 'x' …"),
# ImportError("cannot import name 'f' from 'pkg.mod'"). Redacting those makes
# the row unreadable, so a quoted span is kept when it is identifier-shaped;
# anything else in the same message is still redacted.
_IDENTIFIER_ERRORS = frozenset(
    {"NameError", "UnboundLocalError", "ImportError", "ModuleNotFoundError", "AttributeError"}
)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def bounded_error(error: BaseException | str) -> tuple[str, str]:
    """`(error_class, message)` for a failed request's ledger row.

    The class is the exception type's name (`str` for a plain
    message). The message is the FIRST line only, quoted spans replaced by
    `'…'` so a value the caller sent never lands on the row through an
    exception that echoed it, whitespace collapsed, cut at 200 characters.
    Never raises.
    """
    try:
        cls = type(error).__name__ if isinstance(error, BaseException) else "str"
        text = str(error) if isinstance(error, BaseException) else error
        first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
        if cls in _IDENTIFIER_ERRORS:
            first = _QUOTED_RE.sub(
                lambda m: m.group(0) if _IDENT_RE.match(m.group(0)[1:-1] or "-") else "'…'",
                first,
            )
        else:
            first = _QUOTED_RE.sub("'…'", first)
        first = " ".join(first.split())
        if len(first) > _MAX_ERROR_CHARS:
            first = first[: _MAX_ERROR_CHARS - 1] + "…"
        return cls, first
    except Exception:  # noqa: BLE001 — never drop the event over its error text
        return "unknown", ""


def validation_error_text(errors: list[dict[str, Any]]) -> str:
    """`field: message` per pydantic error, '; '-joined, the failing field
    first so it survives `bounded_error`'s first-line, 200-character bound
    (a VALIDATION_FAILED row says which field). Takes the
    JSON-safe list `validation_errors()` builds for the 422 body, so the
    response and the two ledgers describe the failure in the same words."""
    parts: list[str] = []
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        loc = ".".join(str(x) for x in (err.get("loc") or ())) or "body"
        parts.append(f"{loc}: {err.get('msg') or err.get('type') or 'invalid'}")
    return "; ".join(parts)


def _retrieval_work_columns(work: Any) -> dict[str, str]:
    """The search response's own latency attribution (
    `retrieval_work.stage_ms` per handler stage, `loop_lag_ms` one event-loop
    scheduling sample), flattened onto the event so the ledger can say where a
    slow search spent its time — the shared process or the database — without
    a trace. Five bounded columns: the total, the largest stage by name and
    milliseconds, the loop lag, and the whole partition as
    `out_stage_ms` — `name=ms` pairs, largest first, whole milliseconds, cut
    at the identifier bound so the stages that matter survive the cut. Pure;
    anything odd yields nothing."""
    if not isinstance(work, dict) or not work:
        return {}
    out: dict[str, str] = {}
    try:
        lag = work.get("loop_lag_ms")
        if isinstance(lag, (int, float)) and not isinstance(lag, bool):
            out[RESULT_PREFIX + "loop_lag_ms"] = f"{float(lag):.2f}"
        stages = work.get("stage_ms")
        if isinstance(stages, dict) and stages:
            total = stages.get("total")
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                out[RESULT_PREFIX + "stage_total_ms"] = f"{float(total):.1f}"
            named = [
                (str(k), float(v))
                for k, v in stages.items()
                if k != "total" and isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            if named:
                top_name, top_ms = max(named, key=lambda kv: kv[1])
                out[RESULT_PREFIX + "stage_top"] = top_name[:_MAX_IDENT_CHARS]
                out[RESULT_PREFIX + "stage_top_ms"] = f"{top_ms:.1f}"
                named.sort(key=lambda kv: (-kv[1], kv[0]))
                packed = ",".join(f"{k}={int(round(v))}" for k, v in named)
                out[RESULT_PREFIX + "stage_ms"] = packed[:_MAX_IDENT_CHARS]
    except Exception:  # noqa: BLE001 — telemetry never drops the event
        return {}
    return out


def request_work_columns(work: Any) -> dict[str, str]:
    """The request's own work bag (`core.request_work`) flattened
    onto the row: `out_ch_statements`,
    `out_ch_read_rows`, `out_ch_read_bytes`, `out_ch_elapsed_ms` from the
    response summaries of every statement the request issued;
    `out_injected_facts` / `out_injected_chunks` from identifier injection;
    `schema_skipped` — the tables a pending migration creates that a leg tried
    to read and continued without; and, when the handler marked stages of its
    own and the result carries no `retrieval_work` partition (memory.ask), the
    `out_stage_ms` stage columns from the bag's partition. Pure; anything odd yields
    nothing; a bag that recorded nothing yields nothing."""
    if work is None:
        return {}
    out: dict[str, str] = {}
    try:
        n = int(getattr(work, "ch_statements", 0) or 0)
        if n > 0:
            out[RESULT_PREFIX + "ch_statements"] = str(n)
            out[RESULT_PREFIX + "ch_read_rows"] = str(int(getattr(work, "ch_read_rows", 0) or 0))
            out[RESULT_PREFIX + "ch_read_bytes"] = str(int(getattr(work, "ch_read_bytes", 0) or 0))
            elapsed = float(getattr(work, "ch_elapsed_ms", 0.0) or 0.0)
            out[RESULT_PREFIX + "ch_elapsed_ms"] = f"{elapsed:.1f}"
        facts = int(getattr(work, "injected_facts", 0) or 0)
        chunks = int(getattr(work, "injected_chunks", 0) or 0)
        if facts or chunks:
            out[RESULT_PREFIX + "injected_facts"] = str(facts)
            out[RESULT_PREFIX + "injected_chunks"] = str(chunks)
        skipped = getattr(work, "schema_skipped", None)
        if isinstance(skipped, list) and skipped:
            out["schema_skipped"] = _clip(",".join(str(t) for t in skipped[:_MAX_LIST_ITEMS]))
    except Exception:  # noqa: BLE001 — telemetry never drops the event
        return {}
    return out


def request_work_stage_columns(work: Any) -> dict[str, str]:
    """The bag's own stage partition as the stage columns, for a handler that
    marks stages but whose result has no `retrieval_work` (memory.ask)."""
    try:
        part = work.partition() if work is not None else {}
    except Exception:  # noqa: BLE001 — a projection failure never drops the event
        return {}
    if not part:
        return {}
    return _retrieval_work_columns({"stage_ms": part})


# The plane sections of a sectioned search's answer (`search.query`) whose hits say it
# answered a recall. `profile` is left out: it falls back to matching by meaning, so it
# answers nearly any query; so is `tracker`, the duplicate-check verdict on a defect
# report, which answers whether the defect is filed, not what is remembered.
_SEARCH_SECTIONS = ("memory", "knowledge", "conversations")


def _read_outcome_columns(result: dict[str, Any]) -> dict[str, str]:
    """Whether a read ANSWERED, not just whether it ran. A `memory.get`
    that misses and a `bulk_get` whose keys are not there both return 200 and
    land as `success = 1`, so the ledger alone cannot show misses. `out_found` (true/false) rides a
    single-row read; `out_found_n` / `out_missing_n` ride a bulk read, so a
    found rate per operation per agent is one aggregate over the ledger;
    `out_section_hits` rides a sectioned search, and `out_hits` an assistant op's
    found list (`_hits_column`). Counts only — the keys themselves stay in the response."""
    out: dict[str, str] = {}
    found = result.get("found")
    if isinstance(found, bool):
        out[RESULT_PREFIX + "found"] = "true" if found else "false"
    missing = result.get("missing")
    results = result.get("results")
    if isinstance(missing, list) and isinstance(results, dict):
        out[RESULT_PREFIX + "found_n"] = str(len(results))
        out[RESULT_PREFIX + "missing_n"] = str(len(missing))
    # A sectioned search (`search.query`) answers with a hit list per plane and no top-level
    # `abstain`: the hits across its sections say whether it answered. A section that
    # abstained keeps its below-threshold hits as evidence; they are not an answer.
    sections = [sec for sec in (result.get(k) for k in _SEARCH_SECTIONS) if isinstance(sec, dict)]
    if any("results" in sec for sec in sections):
        hits = sum(
            len(sec["results"])
            for sec in sections
            if isinstance(sec.get("results"), list) and sec.get("abstain") is not True
        )
        out[RESULT_PREFIX + "section_hits"] = str(hits)
    return out


def _hits_column(op_name: str, result: dict[str, Any]) -> dict[str, str]:
    """`out_hits`: how many rows an assistant op's call found, the summed length of the
    output lists `ASSISTANT_FOUND_FIELD` names for it ("0" on an abstention). The
    assistant's label reads it (server/assistant/app/support.py); other ops' rows carry
    none."""
    fields = ASSISTANT_FOUND_FIELD.get(op_name)
    if not fields:
        return {}
    lists = [result.get(f) for f in fields]
    if not any(isinstance(found, list) for found in lists):
        return {}
    hits = sum(len(found) for found in lists if isinstance(found, list))
    return {RESULT_PREFIX + "hits": "0" if result.get("abstain") is True else str(hits)}


def project_result(result: Any, *, op_name: str = "") -> dict[str, str]:
    """Identifier-class fields of a handler's RESULT, prefixed `out_`.

    Temporal policies correlate on what a prior call RETURNED
    (`::response{eventResource: resource}`); a rule like "permit the delete
    only if a prior get returned this memory" needs the get's `memory_id` on
    the event, not just its arguments. Only identifiers are kept (no text
    digests: outputs are read back through the memory plane, not the trace);
    a pydantic model is dumped first. Never raises, always bounded.
    """
    try:
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        if not isinstance(result, dict) or not result:
            return {}
        out: dict[str, str] = {}
        out.update(_retrieval_work_columns(result.get("retrieval_work")))
        out.update(_read_outcome_columns(result))
        out.update(_hits_column(op_name, result))
        for key in sorted(result):
            if key in IDENTITY_KEYS or key not in IDENTIFIER_KEYS:
                continue
            rendered = _identifier(result[key])
            if rendered is not None:
                out[RESULT_PREFIX + str(key)] = rendered
            # The full entry budget, not half of it: memory.ask's result
            # already carries 23 identifier keys, and a cap of 16 would drop the
            # alphabetically-late ones (`pool_k`, `route_*`, `summary_hits`,
            # `timeline_rows`) in silence — a ledger column that exists on the
            # model but never on the row. Arguments keep their
            # own budget; the map stays bounded at _MAX_ENTRIES + _MAX_RESULT_ENTRIES.
            if len(out) >= _MAX_RESULT_ENTRIES:
                break
        return out
    except Exception:  # noqa: BLE001 — never drop the event over its outputs
        return {}


def response_tokens_of(result: Any) -> int:
    """Tokens the agent has to read for this response: len(JSON) / 4, the
    same chars/4 estimate the MCP listing budget uses. A pydantic model is
    dumped first; `None` (a failed
    call, an op with no result) is 0. Bounded to the UInt32 column and never
    raises — a response that will not serialize is 0, not a dropped event."""
    if result is None:
        return 0
    try:
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        text = json.dumps(result, default=str, separators=(",", ":"), ensure_ascii=False)
        return min(len(text) // 4, 0xFFFFFFFF)
    except Exception:  # noqa: BLE001 — the size is telemetry, never a failure
        return 0


__all__ = [
    "IDENTIFIER_KEYS",
    "IDENTITY_KEYS",
    "RESULT_PREFIX",
    "TEXT_KEYS",
    "bounded_error",
    "project_args",
    "project_result",
    "request_work_columns",
    "request_work_stage_columns",
    "response_tokens_of",
]
