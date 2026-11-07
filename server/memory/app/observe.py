"""Ambient-capture handlers — the zero-token episodic write path.

`observe.record` fires on every agent tool call, so it must be one INSERT
into the episodic plane (`memory_type='episodic'`, non-searchable) with no
LLM round-trip and no synchronous embedding. Distillation (extraction,
embedding, conflict) is deferred to the `observe_extract` worker, which
reuses `memory.set_from_messages` at session-end. The blacklist is not: a
step whose content matches a Refuse pattern is stored without its content
(the cache is in-process, so the check costs no round trip once loaded).

`observe.crystallize` is the session-end flush: it tag-rewrites the
session's pending observations to READY so the worker picks them up. It
enqueues; it does not run the LLM.

Idempotency: rows are keyed `obs:{session_id}:{sha8}`, so a re-fired hook
for the same tool call collapses onto the same RMT key.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.hashing import sha256_value as _content_sha256
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_observe import (
    ObserveCrystallizeInput,
    ObserveCrystallizeOutput,
    ObserveRecordInput,
    ObserveRecordOutput,
)
from server.memory.app.provenance import writer_actor
from server.memory.domain.observation_tags import (
    BLACKLISTED_REASON,
    BLACKLISTED_TAG,
    CRYSTALLIZE_DONE_TAG,
    CRYSTALLIZE_READY_TAG,
    OBSERVATION_TAG,
    SELF_REFERENTIAL_REASON,
    SELF_REFERENTIAL_TAG,
    is_self_referential,
)
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.rows import version_over

# Episodic rows are replay fodder for the extractor, not durable facts;
# truncate hard so a long tool_output can't bloat the extraction prompt.
_NARRATIVE_LIMIT = 2000
# Per-field caps so one giant field can't dominate the narrative budget.
_INPUT_LIMIT = 800
_OUTPUT_LIMIT = 800
_ERROR_LIMIT = 600

# Heuristic tool-name → (observation_kind, extra_tags). Substring match,
# case-insensitive — harnesses name tools inconsistently. First match wins,
# so order is specificity-descending (web before generic command, etc.).
_TOOL_RULES: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (
        ("web_fetch", "webfetch", "web_search", "websearch", "fetch_url", "browse"),
        "web_fetch",
        ("web",),
    ),
    (
        (
            "edit",
            "write",
            "str_replace",
            "apply_patch",
            "create_file",
            "update_file",
            "notebook_edit",
            "multiedit",
        ),
        "file_edit",
        ("code",),
    ),
    (("read", "view", "cat", "open_file", "get_file"), "file_read", ("code",)),
    (("bash", "shell", "command", "exec", "run", "terminal", "process"), "command_run", ("shell",)),
)
_ERROR_KIND = "error"
_DEFAULT_KIND = "tool_use"

# File-path extraction. Matches POSIX-ish paths with an extension or a
# leading ./ or / — deliberately conservative so we don't lift every
# slashed string (URLs are handled separately, web tools tag themselves).
_PATH_RE = re.compile(
    r"(?:(?<=[\s\"'(=:])|^)"  # boundary
    r"(\.?/?(?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z0-9]{1,8}"  # dir.../name.ext
    r"|\.?/[\w.\-/]+)"  # or an absolute/relative path without extension
)
# Common keys a tool input dict uses to carry a file target.
_PATH_KEYS = ("file_path", "path", "filename", "file", "notebook_path", "target_file")


def _looks_like_path(p: str) -> bool:
    """A file path, not a URL tail or punctuation. Without this check the narrative sweep
    lifts `//docs.googleapis.com/v1/documents/...` and `//` from curl commands, and those
    become entities and tags. A path has no scheme, does not start with `//`, is at
    least three characters and carries a separator or an extension."""
    if len(p) < 3 or "://" in p or p.startswith("//"):
        return False
    if p in ("/", "./", "../", "."):
        return False
    return "/" in p or "." in p


def _elide(s: str, limit: int) -> str:
    """Truncate to ~`limit` chars keeping BOTH ends, with an explicit marker.
    Tool output often carries its signal at the TAIL — a stack trace's final
    error line, a command's exit status, a diff's tail — which head-only
    truncation silently drops before the extractor ever sees it. ~60% head +
    ~30% tail; the marker tells the model not to read across the cut."""
    if len(s) <= limit:
        return s
    head = int(limit * 0.6)
    tail = int(limit * 0.3)
    elided = len(s) - head - tail
    return f"{s[:head]}…[{elided} chars elided]…{s[-tail:]}"


def _coerce_str(value: Any, limit: int) -> str:
    """Stringify a tool field and truncate (head+tail, see _elide). Compact
    JSON for dicts/lists, None → empty. Never raises."""
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            import json

            s = json.dumps(value, default=str, separators=(",", ":"))
        except Exception:  # noqa: BLE001 — falls back to str()
            s = str(value)
    return _elide(s.strip(), limit)


def _classify_tool(tool_name: str, has_error: bool) -> tuple[str, list[str]]:
    """Map a tool name (+ error presence) to an observation kind and tags.
    A present error payload always wins — a failed edit is a failure
    observation first, an edit second.
    """
    name = (tool_name or "").lower()
    kind = _DEFAULT_KIND
    tags: list[str] = []
    for needles, mapped_kind, extra in _TOOL_RULES:
        if any(n in name for n in needles):
            kind = mapped_kind
            tags = list(extra)
            break
    if has_error:
        # Keep the original kind as a tag so "failed edit" stays discoverable.
        if kind != _DEFAULT_KIND:
            tags.append(kind)
        kind = _ERROR_KIND
        tags.append("error")
    return kind, tags


def _extract_paths(
    tool_input: Any,
    explicit: list[str] | None,
    narrative: str,
) -> list[str]:
    """Lift file paths from (1) the caller's explicit file_paths, (2) the
    well-known path keys of a dict tool_input, (3) a regex sweep over the
    narrative. Deduped, order-preserving, capped. LLM-free."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        p = p.strip().strip("\"'")
        if p and p not in seen and len(p) <= 256 and _looks_like_path(p):
            seen.add(p)
            found.append(p)

    for p in explicit or []:
        if isinstance(p, str):
            _add(p)
    if isinstance(tool_input, dict):
        for key in _PATH_KEYS:
            v = tool_input.get(key)
            if isinstance(v, str):
                _add(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str):
                        _add(item)
    for m in _PATH_RE.finditer(narrative):
        _add(m.group(1))
        if len(found) >= 24:
            break
    return found[:24]


def _build_narrative(
    *,
    tool_name: str,
    kind: str,
    tool_input: Any,
    tool_output: Any,
    error: str | None,
) -> str:
    """Assemble the truncated human-readable narrative stored as the
    episodic value. Deterministic, no LLM. The extractor reads this verbatim
    as one 'turn' when it distills the session."""
    parts = [f"[{kind}] tool={tool_name}"]
    in_s = _coerce_str(tool_input, _INPUT_LIMIT)
    if in_s:
        parts.append(f"input: {in_s}")
    if error:
        parts.append(f"error: {_coerce_str(error, _ERROR_LIMIT)}")
    out_s = _coerce_str(tool_output, _OUTPUT_LIMIT)
    if out_s:
        parts.append(f"output: {out_s}")
    narrative = "\n".join(parts)
    if len(narrative) > _NARRATIVE_LIMIT:
        narrative = _elide(narrative, _NARRATIVE_LIMIT)
    return narrative


def _observation_memory_key(session_id: str, content_sha: str) -> str:
    """Idempotent RMT key for one observation. A re-fired hook for the same
    tool call (same session, same content) collapses onto this key."""
    return f"obs:{session_id}:{content_sha[:8]}"


async def _step_content(input: ObserveRecordInput, kind: str) -> tuple[str, list[str], str]:
    """(narrative, file paths, skipped reason) the row stores for one tool call.
    A self-referential tool (loom itself, a harness meta tool: the input of a
    memory tool IS a memory) and content a blacklist pattern matches keep the
    step, not the content."""
    step_only = f"[{kind}] tool={input.tool_name}"
    if is_self_referential(input.tool_name):
        return step_only, [], SELF_REFERENTIAL_REASON
    narrative = _build_narrative(
        tool_name=input.tool_name,
        kind=kind,
        tool_input=input.tool_input,
        tool_output=input.tool_output,
        error=input.error,
    )
    from server.memory.app import blacklist_guard

    hit = await blacklist_guard.find_match(
        org=input.org, namespace=input.namespace, value=narrative
    )
    if hit is not None:
        # A match: the narrative is at most _NARRATIVE_LIMIT characters, well inside one
        # request's regex budget.
        await blacklist_guard.note_hit(hit.pattern_id)
        return step_only, [], BLACKLISTED_REASON
    return narrative, _extract_paths(input.tool_input, input.file_paths, narrative), ""


async def handle_observe_record(input: ObserveRecordInput) -> ObserveRecordOutput:
    """Zero-token ambient capture. ONE insert into the episodic plane.

    Deliberately NOT `memory.set`: that path runs conflict + entity
    projection (and embedding for searchable types). On the hot path of every
    tool call we bypass them; distillation is the worker's job. The blacklist
    runs here: content matching a Refuse pattern never lands in the row.
    """

    ch = get_client()
    error = input.error
    has_error = bool(error and str(error).strip())
    kind, kind_tags = _classify_tool(input.tool_name, has_error)
    narrative, paths, skipped = await _step_content(input, kind)
    self_referential = skipped == SELF_REFERENTIAL_REASON
    blacklisted = skipped == BLACKLISTED_REASON
    if skipped:
        has_error = False

    # Structured value so the extractor (and dashboard) sees shape, not a
    # blob; this is what the session-end extractor replays.
    value: dict[str, Any] = {
        "kind": kind,
        "tool_name": input.tool_name,
        "narrative": narrative,
        "file_paths": paths,
    }
    if has_error:
        value["error"] = _coerce_str(error, _ERROR_LIMIT)

    content_sha = _content_sha256(value)
    memory_key = _observation_memory_key(input.session_id, content_sha)

    # Marker + kind + kind-tags + file basenames (so search-by-tag finds
    # "everything that touched config.py") + caller tags. PENDING by default.
    tags = [OBSERVATION_TAG, kind, *kind_tags]
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if base and base not in tags:
            tags.append(base)
    for t in input.tags or []:
        if t and t not in tags:
            tags.append(t)
    _action = str(input.action_type or "").strip()
    if _action and f"action:{_action}" not in tags:
        tags.append(f"action:{_action}")
    if self_referential:
        # Already DONE: the crystallize scan and the worker read neither.
        tags.extend((SELF_REFERENTIAL_TAG, CRYSTALLIZE_DONE_TAG))
    elif blacklisted:
        tags.extend((BLACKLISTED_TAG, CRYSTALLIZE_DONE_TAG))
    tags = list(dict.fromkeys(tags))[:32]

    entities = list(paths)

    now_naive = naive_utc_now()
    import json as _json

    # An observation with no date is dated at the request time.
    from server.core.datetimes import parse_iso_dt
    from server.memory.domain.observation_date import resolve_observation_date
    from server.settings import settings as _od_settings

    observation_date, _od_source = resolve_observation_date(
        input.observation_date or input.observed_at,
        default=str(_od_settings.memory_extract_observation_date_default),
    )
    valid_at: datetime | None = parse_iso_dt(observation_date) if observation_date else None

    serialized = _json.dumps(value)
    import server.memory.store as memory_repo

    row = memory_repo.build_first_version_row(
        {
            "org": input.org,
            "namespace": input.namespace,
            "agent": input.agent,
            "user_id": input.user_id,
            "session_id": input.session_id,
            "run_id": input.run_id,
            "memory_key": memory_key,
            "memory_type": "episodic",
            "value": serialized,
            "value_json": serialized,
            "confidence": 1.0,
            "source_trace": "observe.record",
            "actor": writer_actor(),
            "source_class": "observed",
            "version": memory_repo.next_version(),
            "valid_at": valid_at,
            "tags": tags,
            "entities": entities,
            "content_sha256": content_sha,
        },
        now=now_naive,
    )

    await insert_memory_versions([row], ch=ch)

    return ObserveRecordOutput(
        memory_key=memory_key,
        memory_id=str(row["memory_id"]),
        kind=kind,
        tags=tags,
        file_paths=paths,
        content_sha256=content_sha,
        tool_name=input.tool_name,
        has_error=has_error,
        narrative_chars=len(narrative),
        skipped_reason=skipped,
    )


async def handle_observe_crystallize(
    input: ObserveCrystallizeInput,
) -> ObserveCrystallizeOutput:
    """Session-end flush. Mark a session's pending episodic observations
    READY so the deferred-extraction worker distills them. Enqueues only.

    Reads the live PENDING rows (tagged `observation`, not yet READY/DONE),
    then writes a new RMT version of each with the READY tag added. Every
    column is mirrored via build_memory_version_row (the RMT
    mirror-every-column discipline) so the collapse keeps the row whole.
    """
    import server.memory.store as memory_repo

    ch = get_client()

    # FINAL collapses the RMT so we read current tag state.
    rows = await ch.query_all(
        """
        SELECT memory_key
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND user_id = {uid:String}
          AND session_id = {sid:String}
          AND memory_type = 'episodic'
          AND has(tags, {obs_tag:String})
          AND NOT has(tags, {ready_tag:String})
          AND NOT has(tags, {done_tag:String})
          AND tombstoned_at IS NULL
        ORDER BY created_at ASC
        LIMIT {lim:UInt32}
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "agent": input.agent,
            "uid": input.user_id,
            "sid": input.session_id,
            "obs_tag": OBSERVATION_TAG,
            "ready_tag": CRYSTALLIZE_READY_TAG,
            "done_tag": CRYSTALLIZE_DONE_TAG,
            "lim": input.max_observations,
        },
    )
    keys = [str(r["memory_key"]) for r in rows]
    if not keys:
        return ObserveCrystallizeOutput(enqueued=0, session_id=input.session_id)

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )

    enqueued = 0
    for key in keys:
        # get_by_key returns the full row; build_memory_version_row needs
        # every column to mirror forward. The bulk get_many_by_keys projects
        # a subset and would zero tags/entities on the rewrite.
        existing = await memory_repo.get_by_key(identity, key, fresh=True, scope=WHOLE_NAMESPACE)
        if existing is None:
            continue
        new_tags = list(existing.tags or [])
        if CRYSTALLIZE_READY_TAG not in new_tags:
            new_tags.append(CRYSTALLIZE_READY_TAG)
        version_row = memory_repo.build_memory_version_row(
            existing,
            version=version_over(existing),
            tags=new_tags,
        )
        await memory_repo.insert_versions_over_heads([version_row], [existing], ch=ch)
        enqueued += 1

    return ObserveCrystallizeOutput(enqueued=enqueued, session_id=input.session_id)
