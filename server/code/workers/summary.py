"""Weekly LLM file/module summary cards for code.map orientation.

Candidates are ordered by merged-PR churn over the source tree, filled from
the include-graph PageRank once the churn list runs out, and gated by `inputs_hash` (a file
whose content hash hasn't changed keeps its card verbatim — no LLM spend),
bounded per pass. One card per file: rows a previous embedder left under
its own model name are tombstoned once the active model has the file, and
their card text is carried over first when it still matches the file.
Cards are embedded inline; failures leave a dim=0 stub the
code_embed_recovery worker fills.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.index.card_selection import (
    churn_eligible,
    select_card_paths,
    split_csv,
    twin_rows,
)
from server.llm import customer
from server.observability.genai import staged
from server.settings import settings
from server.settings.budgets import llm_max_tokens
from server.workers import pass_context
from server.workers.status import record_pass, set_worker_identity

logger = logging.getLogger("loom.workers.code_summary")

_SYSTEM = (
    "You write orientation cards for a codebase index. Given code excerpts "
    "from one file, reply with 2-3 sentences: what the file is responsible "
    "for, its key types/functions, and how it relates to its module. Plain "
    "prose, no preamble, no code fences."
)

_BATCH = 5000


@dataclass
class CodeSummaryResult:
    cards_written: int = 0
    cards_carried: int = 0
    skipped_unchanged: int = 0
    twins_tombstoned: int = 0
    errors: list[str] = field(default_factory=list)


@staged("code_summary")
async def _summarize(llm: Any, path: str, excerpts: str) -> str:
    res = await llm.complete(
        _SYSTEM,
        f"File: {path}\n\n{excerpts}",
        max_tokens=llm_max_tokens("code_summary"),
    )
    return str(res.text).strip()[:1500]


async def run_pass() -> CodeSummaryResult:
    result = CodeSummaryResult()
    with record_pass("code_summary") as rec:
        await _run(result)
        rec.outcome = {
            "cards_written": result.cards_written,
            "cards_carried": result.cards_carried,
            "skipped_unchanged": result.skipped_unchanged,
            "twins_tombstoned": result.twins_tombstoned,
            "errors": len(result.errors),
        }
    return result


async def _run(result: CodeSummaryResult) -> None:

    from server.code.domain.index.repos import configured_repos

    repos = list(configured_repos())
    cap = int(settings.code_summary_max_scopes_per_pass)
    for i, cfg in enumerate(repos):
        # The cap is the pass's, shared out: each repository may write up to
        # an even share of what is left, and what one does not use rolls to
        # the ones after it. One repository with thousands of owed cards
        # cannot hold the rest back for a week.
        left = cap - result.cards_written
        if left <= 0:
            break
        share = max(1, -(-left // (len(repos) - i)))
        if await customer.switched_off(cfg.org):
            continue  # its cards wait until the org is switched back on
        try:
            with customer.for_org(cfg.org):
                await _summarize_repo(
                    cfg.org, cfg.namespace, result, cap=result.cards_written + share
                )
        except customer.HOLDS:
            continue  # a held call is no failure: its cards wait until the org is back
        except Exception as e:  # one repository's failed read never skips the rest
            logger.exception("code_summary: %s/%s failed", cfg.org, cfg.namespace)
            result.errors.append(f"{cfg.namespace}: {type(e).__name__}: {e}")
            pass_context.note_failure(e, cfg.namespace)


def upgradable(live: dict[str, Any]) -> bool:
    """An unchanged file's card is rewritten when it was
    written by a model the deployment lists as upgradable and the configured
    model is another one; a card from the configured model, or from a model
    not listed, stays."""
    current = str(settings.code_summary_model).strip()
    wrote = str(live.get("summary_model") or "").strip()
    if not current or not wrote or wrote == current:
        return False
    return wrote in split_csv(settings.code_summary_upgrade_from_models)


def _hash_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").rstrip("\x00")
    return str(value or "")


async def _candidate_order(
    repo: Any,
    org: str,
    ns: str,
    summaries: dict[str, dict[str, Any]],
    file_hashes: dict[str, str],
) -> list[str]:
    """Every live file in the order the pass considers it (the per-pass cap
    counts cards written, not files looked at, so the list is not cut)."""
    pagerank_order = [
        str(r["scope_path"])
        for r in sorted(summaries.values(), key=lambda r: -float(r.get("pagerank") or 0.0))
    ]
    prefixes = split_csv(settings.code_summary_churn_prefixes)
    exclude = split_csv(settings.code_summary_churn_exclude)
    ranked = await repo.pr_churn_ranked(
        org,
        ns,
        days=settings.code_summary_churn_days,
        prefixes=prefixes,
        exclude=exclude,
    )
    churn_order = [
        p
        for p, _ in ranked
        if p in file_hashes and churn_eligible(p, prefixes=prefixes, exclude=exclude)
    ]
    return select_card_paths(
        pagerank_order=pagerank_order,
        churn_order=churn_order,
        limit=len(pagerank_order) + len(churn_order),
    )


async def _dedupe_models(
    repo: Any,
    embedder: Any,
    org: str,
    ns: str,
    model: str,
    summaries: dict[str, dict[str, Any]],
    file_hashes: dict[str, str],
    result: CodeSummaryResult,
) -> None:
    """Tombstone live rows under other models once the active model has the
    file. A twin whose card text still matches the file's content hash is
    carried into the active row first (embed only — no LLM), bounded by the
    pass cap; twins past the cap keep their text for the next pass."""
    others = await repo.live_summary_rows_other_models(org, ns, model, with_embedding=False)
    twins = twin_rows(list(summaries.values()) + list(others), active_model=model)
    if not twins:
        return
    from server.code.store.index import CodeRepo

    carried = 0
    tombstones: list[dict[str, Any]] = []
    for twin in twins:
        path = str(twin["scope_path"])
        live = summaries.get(path)
        content_hash = file_hashes.get(path, "")
        reusable = (
            live is not None
            and str(twin.get("summary") or "")
            and content_hash
            and _hash_str(twin.get("inputs_hash")) == content_hash
            and not str(live.get("summary") or "")
        )
        if reusable:
            if carried >= settings.code_summary_max_scopes_per_pass:
                continue  # keep the twin; its text is carried next pass
            try:
                assert live is not None
                row = await _card_row(
                    embedder,
                    live,
                    str(twin["summary"]),
                    str(twin.get("summary_model") or ""),
                    content_hash,
                )
                await repo.insert_rows("code_summaries", [row])
                summaries[path] = row
                carried += 1
            except Exception as e:  # noqa: BLE001 — per-file, keep going
                result.errors.append(f"{path}: carry: {type(e).__name__}: {e}")
                continue
        tombstones.append(CodeRepo.tombstone_of("code_summaries", twin))
    for start in range(0, len(tombstones), _BATCH):
        await repo.insert_rows("code_summaries", tombstones[start : start + _BATCH])
    result.cards_carried += carried
    result.twins_tombstoned += len(tombstones)


async def _card_row(
    embedder: Any,
    live: dict[str, Any],
    summary: str,
    summary_model: str,
    content_hash: str,
) -> dict[str, Any]:
    from server.code.store.index import CodeRepo

    vec: list[float] = []
    try:
        vec = await embedder.embed(summary)
    except customer.HOLDS:
        pass  # a stub: the recovery worker embeds it once the org is back on
    except Exception as e:  # noqa: BLE001 — recovery worker fills stubs
        logger.warning("code_summary: embed failed for %s: %s", live.get("scope_path"), e)
    row = CodeRepo.tombstone_of("code_summaries", live)  # full mirror
    row["tombstoned_at"] = None
    row["summary"] = summary
    row["summary_model"] = summary_model
    row["inputs_hash"] = content_hash
    row["embedding"] = vec
    row["embedding_dim"] = len(vec)
    row["embed_last_error"] = ""
    row["embed_failed_at"] = None
    return row


async def _summarize_repo(
    org: str, ns: str, result: CodeSummaryResult, *, cap: int | None = None
) -> None:
    """One repository's cards, until the pass has written `cap` in total
    (None → the whole per-pass cap)."""
    from server.code.store.index import CodeRepo, build_row
    from server.embedding.embedder import get_embedder
    from server.llm.client import get_llm_client_for_model
    from server.observability import cost_accumulator
    from server.observability.budget import namespace_over_cap
    from server.workers.cost_event import emit_worker_cost_event_for

    set_worker_identity("code_summary", org=org, namespace=ns)
    if await namespace_over_cap(org, ns):
        result.errors.append("namespace over budget cap — pass skipped")
        return

    repo = CodeRepo()
    embedder = get_embedder()
    llm = get_llm_client_for_model(settings.code_summary_model or None)
    model = embedder.model_name

    file_hashes = await repo.live_file_hashes(org, ns)
    summaries = {
        str(r["scope_path"]): r
        for r in await repo.live_summary_rows(org, ns, model, with_embedding=False)
        if str(r["scope_type"]) == "file"
    }
    await _dedupe_models(repo, embedder, org, ns, model, summaries, file_hashes, result)

    candidates = await _candidate_order(repo, org, ns, summaries, file_hashes)
    if cap is None:
        cap = int(settings.code_summary_max_scopes_per_pass)
    # Cards are written `concurrency` at a time (default 1 = a sequential
    # pass). A first-day pass over a
    # 30k-file repository is 2 s per card sequentially; eight in flight is the
    # embedder's and the fast model's comfortable rate. The cap counts cards
    # written and is checked before each file starts, so a pass may overshoot
    # it by at most `concurrency - 1`. It counts every card this PASS wrote
    # (`result.cards_written`), across repositories.
    workers = max(1, int(settings.code_summary_concurrency))
    sem = asyncio.Semaphore(workers)

    async def _one(path: str) -> None:
        if result.cards_written >= cap:
            return
        content_hash = file_hashes.get(path, "")
        live = summaries.get(path)
        if live is None:
            # A churn-selected file the graph-stats pass has not ranked yet.
            live = build_row(
                "code_summaries",
                org=org,
                namespace=ns,
                embedding_model=model,
                scope_type="file",
                scope_path=path,
            )
        inputs_hash = _hash_str(live.get("inputs_hash"))
        unchanged = bool(content_hash and inputs_hash == content_hash and live.get("summary"))
        if unchanged and not upgradable(live):
            result.skipped_unchanged += 1
            return
        async with sem:
            if result.cards_written >= cap:
                return
            started = time.monotonic()
            cost_accumulator.begin_op()
            try:
                if await _write_card(
                    repo, llm, embedder, live, org=org, ns=ns, path=path, content_hash=content_hash
                ):
                    result.cards_written += 1
            except customer.HOLDS:
                raise  # the org's calls are held: the pass leaves the rest of this repository
            except Exception as e:  # noqa: BLE001 — per-file, keep going
                result.errors.append(f"{path}: {type(e).__name__}: {e}")
            finally:
                emit_worker_cost_event_for(
                    org=org,
                    namespace=ns,
                    operation="worker.code_summary",
                    started=started,
                )

    await _run_cards(candidates, _one, workers, lambda: result.cards_written >= cap)


async def _write_card(
    repo: Any,
    llm: Any,
    embedder: Any,
    live: dict[str, Any],
    *,
    org: str,
    ns: str,
    path: str,
    content_hash: str,
) -> bool:
    """One file's card, summarised, embedded and written; False with no excerpts."""
    excerpts = await _file_excerpts(repo, org, ns, path, embedder.model_name)
    if not excerpts:
        return False
    summary = await _summarize(llm, path, excerpts)
    row = await _card_row(embedder, live, summary, getattr(llm, "model", ""), content_hash)
    await repo.insert_rows("code_summaries", [row])
    return True


async def _run_cards(
    candidates: list[str],
    one: Any,
    workers: int,
    at_cap: Any,
) -> None:
    """Consume the candidate list in order until `at_cap()`: sequentially
    for one worker, else as a bounded fan-out with `workers` files in flight.
    A file that raises (a held model call) ends the list, its peers cancelled."""
    if workers == 1:
        for path in candidates:
            if at_cap():
                break
            await one(path)
        return
    pending: list[asyncio.Task[None]] = []
    try:
        for path in candidates:
            if at_cap():
                break
            pending.append(asyncio.create_task(one(path)))
            if len(pending) >= workers * 2:
                done_set, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                pending = [t for t in pending if t not in done_set]
                # Every finished file's error is read, the first re-raised.
                errors = [t.exception() for t in done_set if not t.cancelled()]
                if first := next((e for e in errors if e is not None), None):
                    raise first
        if pending:
            await asyncio.gather(*pending)
    finally:
        # `asyncio.wait` leaves its tasks running on a cancel or a raise:
        # cancel and await them so none outlives the pass.
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def _file_excerpts(repo: Any, org: str, ns: str, path: str, model: str) -> str:
    rows = await repo.live_chunks_for_path(org, ns, path, model)
    rows.sort(key=lambda r: int(r.get("start_line") or 0))
    parts: list[str] = []
    budget = 6000
    for r in rows:
        scope = str(r.get("scope") or "")
        content = str(r.get("content") or "")
        head = content[:600]
        parts.append(f"// {scope}\n{head}")
        budget -= len(head)
        if budget <= 0:
            break
    return "\n\n".join(parts)
