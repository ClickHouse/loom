"""code.setting_facts: one setting, the four places that
define it, one bounded round of keyed reads.

Declaration and tier from the settings files, the change history with the
release of each change (the history file parsed end to end from its
chunks, cached per process), whether `tests/clickhouse-test` randomizes it
in CI and with which sampler, and how many test files name it. A positive
control — the declaration of `LOOM_CODE_SETTING_CONTROL` (default
`max_threads`) — runs with every call: `not_a_setting` is claimed only when
the control found its own declaration in the same index.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

from server.code.api.models_conventions import CodeSettingFactsOutput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.spans import code_span, set_code_span_attributes
from server.code.app.ttl_cache import TtlCache
from server.code.domain.index import setting_facts as sf
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)

_history_cache: TtlCache[str, list[dict[str, Any]]] = TtlCache(600.0, maxsize=256)


async def history_chunks(repo: CodeRepo, org: str, ns: str) -> list[dict[str, Any]]:
    """SettingsChangesHistory.cpp end to end, cached ten minutes per
    namespace; `code.conventions` reads the same parse."""
    key = f"{org}|{ns}"
    hit = _history_cache.get(key)
    if hit is not None:
        return hit
    rows = await repo.chunks_of_path(org, ns, sf.HISTORY_FILE)
    _history_cache.put(key, rows)
    return rows


async def _declaration(repo: CodeRepo, org: str, ns: str, name: str) -> sf.Declaration | None:
    """The DECLARE of `name` in any declaration file, else its MAKE_OBSOLETE."""
    # the primary form, and the alias form — `name` as the macro's last argument, right after
    # the flags word (a bare `, name)` matches every C++ call site in src/ and drowns the
    # declaration files); one read per declaration directory, gathered
    pattern = (
        r"DECLARE(_WITH_ALIAS)?\(\w+, "
        + name
        + r","
        + r"|, (0|IMPORTANT|BETA|EXPERIMENTAL), "
        + name
        + r"\)"
    )
    batches = await asyncio.gather(
        *(
            repo.grep_chunks(org, ns, pattern, regex=True, path_prefix=prefix, limit=6)
            for prefix in ("src/Core/", "src/Storages/MergeTree/")
        )
    )
    rows = [r for batch in batches for r in batch]
    for r in rows:
        path = str(r.get("path") or "")
        if path not in sf.DECLARATION_FILES:
            continue
        d = sf.parse_declaration(
            str(r.get("content") or ""), name, path=path, chunk_start=int(r.get("start_line") or 0)
        )
        if d is not None:
            return d
    rows = await repo.grep_chunks(
        org,
        ns,
        r"MAKE_OBSOLETE\(M, \w+, " + name + ",",
        regex=True,
        path_prefix="src/Core/",
        limit=4,
    )
    for r in rows:
        d = sf.parse_obsolete(
            str(r.get("content") or ""),
            name,
            path=str(r.get("path") or ""),
            chunk_start=int(r.get("start_line") or 0),
        )
        if d is not None:
            return d
    return None


async def _control(repo: CodeRepo, org: str, ns: str) -> tuple[bool, str]:
    probe = str(settings.code_setting_control)
    if not probe:  # no control configured: the absence of a name proves nothing
        return False, ""
    try:
        return (await _declaration(repo, org, ns, probe)) is not None, probe
    except Exception as e:  # noqa: BLE001 — a failed control is a failed control
        logger.info("setting_facts control failed: %s", e)
        return False, probe


async def handle_code_setting_facts(input: Any) -> Any:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    name = input.name.strip()
    facts = sf.Facts(name=name)
    if not sf.valid_name(name):
        return CodeSettingFactsOutput(name=name, verdict="not_a_setting", causes=["invalid_name"])
    with code_span("setting_facts.reads", seeds=1):
        gathered = await asyncio.gather(
            _control(repo, org, ns),
            _declaration(repo, org, ns, name),
            history_chunks(repo, org, ns),
            repo.chunk_contains(org, ns, sf.RANDOMIZER_FILE, f'"{name}":', limit=4),
            repo.grep_files(org, ns, name, path_prefix="tests/", limit=1),
            return_exceptions=True,
        )
    control_ok, probe = (
        cast(tuple[bool, str], gathered[0])
        if not isinstance(gathered[0], BaseException)
        else (False, "")
    )
    decl = gathered[1] if not isinstance(gathered[1], BaseException) else None
    hist_rows = gathered[2] if not isinstance(gathered[2], BaseException) else []
    rand_rows = gathered[3] if not isinstance(gathered[3], BaseException) else []
    grep_res = gathered[4] if not isinstance(gathered[4], BaseException) else ([], 0)
    for g in gathered:
        if isinstance(g, BaseException):
            logger.info("setting_facts read failed (open): %s", g)
    facts.declaration = cast(sf.Declaration | None, decl)
    facts.history = sf.parse_history(cast(list[dict[str, Any]], hist_rows), name)
    facts.randomization = sf.parse_randomization(cast(list[dict[str, Any]], rand_rows), name)
    facts.tests_total = int(cast(tuple[Any, int], grep_res)[1] or 0)
    verdict, causes = sf.verdict_of(facts, control_ok=control_ok)
    if isinstance(gathered[2], BaseException):
        causes = [*causes, "history_unread"]
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.setting_facts",
        query=name,
        route="setting_facts",
        returned_ids=[],
        returned_paths=[facts.declaration.file] if facts.declaration else [],
        returned_scores=[],
        top_k=1,
        tokens_returned=0,
        abstain=verdict == "unknown",
        retrieval_risk="low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(
        route="setting_facts", results=int(verdict == "known"), consumer=input.consumer
    )
    return CodeSettingFactsOutput(
        name=name,
        verdict=verdict,
        causes=causes,
        declaration=facts.declaration.as_dict() if facts.declaration else {},
        tier=facts.declaration.tier if facts.declaration else "",
        history=[h.as_dict() for h in facts.history[: input.history_limit]],
        randomized_in_ci=facts.randomization.as_dict(),
        tests_total=facts.tests_total,
        control={"setting": probe, "ok": control_ok},
        trace_run_id=trace_run_id,
    )
