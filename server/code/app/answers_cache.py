"""The code.ask answer cache: the exact, semantic and negative lookups, reuse from the
trunk's entry, the guard that a cached answer's load-bearing tokens still hold, and
the entry's write and tombstone."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from server.code.app.answers_entry import (
    AskResult,
    _AskCtx,
    cache_hit_threshold,
    cache_namespace_ok,
    entry_id,
    normalize_question,
)
from server.code.store.index import CodeRepo, build_row
from server.settings import settings

logger = logging.getLogger(__name__)


# Nearest cached questions a lookup considers: the first may be a sibling
# key's entry, a guarded near-miss or a dead version, and the next may hit.
_CANDIDATES = 5
# Files the synthesis was shown (beyond the cited ones) an entry records.
_MAX_RETRIEVAL_PATHS = 12


# ---- the identifier guard -------------------------------------------------
# A paraphrase keeps the identifiers of its question; a different question
# that sits close in embedding space usually differs in one ("where is
# max_threads applied" / "where is max_insert_threads applied" cosine 0.8797
# on text-embedding-3-large). Before a semantic hit is served the two
# questions' load-bearing tokens must be the same set. Order matters: each
# pattern consumes its span, so `tracker.duplicate_check` is one dotted
# token, not also a snake_case one.
_GUARD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"`([^`]+)`"),  # `quoted`
    re.compile(r"(?<![\w])\"([^\"]+)\""),  # "quoted"
    re.compile(r"(?<![\w])'([^']+)'(?![\w])"),  # 'quoted'
    re.compile(r"(?<![\w/.])(?:[\w.-]+/)+[\w.-]*\w"),  # file paths
    re.compile(r"\b\w+(?:::\w+)+"),  # a::b names
    re.compile(r"\b[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*|\*))+"),  # dotted names, tracker.*
    re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b"),  # snake_case, ERROR_CODES
    re.compile(r"\b(?:[a-z]+[A-Z]|[A-Z][a-z0-9]+[A-Z])\w*"),  # camelCase, PascalCase
    re.compile(r"\b[A-Za-z]+\d\w*"),  # S3, gpt4
    re.compile(r"(?<![\w.])#?\d+(?:\.\d+)*\b"),  # 26.8, #119663, 3
)
# Names of the indexed products themselves: every question may say them.
_GUARD_STOP = frozenset({"clickhouse", "loom", "e.g", "i.e", "etc"})


def load_bearing_tokens(text: str) -> frozenset[str]:
    """The tokens that make a question about ONE thing: code identifiers
    (snake_case, camelCase, `::` and dotted names), setting names, file
    paths, numbers / versions / PR and issue numbers, quoted strings and
    error codes — lower-cased, `#` and trailing dots dropped."""
    rest = text or ""
    out: set[str] = set()
    for pat in _GUARD_PATTERNS:

        def _take(m: re.Match[str]) -> str:
            tok = (m.group(1) if m.groups() else m.group(0)).strip().lower()
            tok = tok.lstrip("#").rstrip(".")
            if tok and tok not in _GUARD_STOP:
                out.add(tok)
            return " " * len(m.group(0))

        rest = pat.sub(_take, rest)
    return frozenset(out)


def guard_passes(asked: str, cached: str) -> bool:
    return load_bearing_tokens(asked) == load_bearing_tokens(cached)


# ---- read path --------------------------------------------------------------


@dataclass
class _Lookup:
    outcome: str = "miss"
    result: AskResult | None = None


async def _serve_from_cache(ctx: _AskCtx) -> _Lookup:
    """Own namespace's semantic entry, else the exact question's cached
    abstain, else the release branch's trunk entry. Fails open to a miss."""
    try:
        (outcome, row, sim), negative = await asyncio.gather(
            _semantic(ctx, ctx.ns, verify_ns=ctx.ns, own=True),
            _negative(ctx),
        )
    except Exception as e:  # noqa: BLE001 — a cache read never sinks the ask
        logger.debug("code.ask cache read failed: %s", e)
        return _Lookup("miss")
    if row is not None:
        return _Lookup("hit", _served(row, sim, "hit", ctx.ns))
    if negative is not None:
        return _Lookup("negative", negative)
    reused = await _reuse_from_trunk(ctx)
    return reused or _Lookup(outcome)


def _served(row: dict[str, Any], sim: float, outcome: str, ns: str) -> AskResult:
    answer = str(row.get("answer") or "")
    return AskResult(
        answer=answer,
        citations=json.loads(str(row.get("citations") or "") or "[]"),
        source="cached",
        cache_similarity=round(sim, 4),
        tokens_returned=len(answer) // 4,
        cache=outcome,
        cache_saved_ms=int(row.get("latency_ms") or 0),
        cache_saved_usd=round(float(row.get("cost_usd") or 0.0), 6),
        cache_entry={"namespace": ns, "question_id": str(row.get("question_id") or "")},
    )


async def _cache_lookup(
    repo: CodeRepo, org: str, ns: str, model: str, qvec: list[float]
) -> list[dict[str, Any]]:
    """The nearest `_CANDIDATES` live answer entries of (org, ns, model).
    Never reads an unresolved namespace."""
    if not cache_namespace_ok(ns):
        return []
    return await repo.nearest_answers(org, ns, model, qvec, k=_CANDIDATES)


def _verdict(ctx: _AskCtx, row: dict[str, Any]) -> str:
    """dead (another prompt version, a row with no prompt version, past the max age) |
    key_mismatch | guarded | ok."""
    if str(row.get("prompt_version") or "") != ctx.key.prompt_version:
        return "dead"
    if str(row.get("cache_key") or "") != ctx.key.digest():
        return "key_mismatch"
    max_age = int(settings.code_ask_cache_max_age_days) * 86400
    if max_age and int(row.get("age_s") or 0) > max_age:
        return "dead"
    if not guard_passes(ctx.question, str(row.get("question") or "")):
        return "guarded"
    return "ok"


async def _semantic(
    ctx: _AskCtx, ns: str, *, verify_ns: str, own: bool
) -> tuple[str, dict[str, Any] | None, float]:
    """(outcome, served row or None, its similarity). The outcome of a miss
    is the nearest above-threshold candidate's verdict (stale | guarded |
    key_mismatch), `miss` when none clears the threshold. `verify_ns` is the
    namespace whose files must still hash as the entry recorded; `own`
    tombstones what is dead or stale (a trunk entry read for a branch is
    only borrowed)."""
    rows = await _cache_lookup(ctx.repo, ctx.org, ns, ctx.model, ctx.qvec)
    threshold = cache_hit_threshold(ctx.model)
    outcome = ""
    for row in rows:
        sim = 1.0 - float(row.get("distance") or 0.0)
        if sim < threshold:
            break
        verdict = _verdict(ctx, row)
        if verdict == "ok":
            if await _files_current(ctx.repo, ctx.org, verify_ns, row):
                return "hit", row, sim
            verdict = "stale"
        elif verdict == "dead":
            verdict = "stale"
        if verdict == "stale" and own:
            await _tombstone_entry(ctx.repo, row)  # cited / shown code changed
        outcome = outcome or verdict
    return outcome or "miss", None, 0.0


async def _negative(ctx: _AskCtx) -> AskResult | None:
    """The exact question's cached abstain: same key, same index head, younger than the
    negative TTL. Never matched semantically."""
    ttl = int(settings.code_ask_negative_cache_ttl_s)
    if not ttl or not ctx.index_head:
        return None
    row = await ctx.repo.negative_answer(
        ctx.org, ctx.ns, ctx.model, entry_id(ctx.question, ctx.key.digest(), "negative")
    )
    if not row:
        return None
    if normalize_question(str(row.get("question") or "")) != normalize_question(ctx.question):
        return None
    if _fixed(row.get("indexed_commit")) != ctx.index_head or int(row.get("age_s") or 0) > ttl:
        return None
    return AskResult(
        source="abstained",
        abstain=True,
        cache="negative",
        abstain_reason=str(row.get("abstain_reason") or "") or "no strong match in the code index",
    )


# ---- cross-branch reuse ---------------------------------------------------


def _trunk_of(org: str, ns: str) -> str:
    try:
        from server.code.domain.index.repos import repo_for_namespace

        cfg = repo_for_namespace(org, ns)
    except Exception:  # noqa: BLE001 — no repo config: no trunk to reuse
        return ""
    src = cfg.embed_reuse_from.strip() if cfg else ""
    return src if src and src != ns and cache_namespace_ok(src) else ""


async def reuse_allowed(org: str, branch: str, trunk: str) -> bool:
    """A trunk entry may answer a branch ask only inside one access boundary: both
    restricted or neither, the same visibility, and the caller able to read the trunk.
    Anything unknown refuses: never a restricted answer in a public namespace, nor the
    reverse."""
    try:
        from server.auth.authz import row_in_scope
        from server.identity.app.namespace_classification import is_restricted
        from server.identity.app.namespace_visibility import visibility_map
        from server.operations.base import Role

        if await is_restricted(org, branch) != await is_restricted(org, trunk):
            return False
        vis = await visibility_map(org, [branch, trunk])
        if not vis.get(branch) or vis.get(branch) != vis.get(trunk):
            return False
        return bool(await row_in_scope(org, trunk, Role.VIEWER))
    except Exception as e:  # noqa: BLE001 — fail closed
        logger.debug("code.ask cross-branch reuse refused: %s", e)
        return False


async def _reuse_from_trunk(ctx: _AskCtx) -> _Lookup | None:
    """A release-branch namespace that misses reads its `embed_reuse_from` trunk's
    cache; the entry is served only when every cited and shown file hashes the same in
    the BRANCH, and a copy is stored in the branch so the next ask hits there."""
    trunk = _trunk_of(ctx.org, ctx.ns)
    if not trunk or not await reuse_allowed(ctx.org, ctx.ns, trunk):
        return None
    try:
        outcome, row, sim = await _semantic(ctx, trunk, verify_ns=ctx.ns, own=False)
    except Exception as e:  # noqa: BLE001 — the trunk cache is an optimization: a miss
        logger.debug("code.ask trunk cache read failed: %s", e)
        return None
    if row is None:
        return None
    label = f"reused_from:{trunk}"
    await _store(
        ctx,
        _entry_row(
            ctx,
            answer=str(row.get("answer") or ""),
            citations=str(row.get("citations") or "[]"),
            cited=_paths_hashes(row, "cited"),
            shown=_paths_hashes(row, "retrieval"),
            llm_model=str(row.get("llm_model") or ""),
            confidence=float(row.get("retrieval_confidence") or 0.0),
            cost_usd=float(row.get("cost_usd") or 0.0),
            latency_ms=int(row.get("latency_ms") or 0),
            reused_from=trunk,
        ),
    )
    served = _served(row, sim, label, ctx.ns)
    served.cache_entry = {
        "namespace": ctx.ns,
        "question_id": entry_id(ctx.question, ctx.key.digest()),
        "trunk_namespace": trunk,
        "trunk_question_id": str(row.get("question_id") or ""),
    }
    return _Lookup(label, served)


# ---- write path -------------------------------------------------------------


def _request_cost() -> float:
    try:
        from server.observability.cost_accumulator import current

        acc = current()
        return float(acc.cost_usd) if acc is not None else 0.0
    except Exception:  # noqa: BLE001 — cost accounting never fails the ask
        return 0.0


def _pre_synthesis_cache_on() -> bool:
    """The identifier proposal and the HyDE / concept legs go through the exact-prompt
    response cache unless the setting or the request's traffic class turns it off."""
    if not bool(settings.code_ask_llm_cache):
        return False
    try:
        from server.identity.domain.traffic_class_policy import llm_policy_for
        from server.observability.traffic_class import current_traffic_class

        pinned = llm_policy_for(current_traffic_class()).llm_cache
    except Exception:  # noqa: BLE001 — no traffic-class pin: the setting decides
        pinned = None
    return True if pinned is None else bool(pinned)


def _exact_cached(llm: Any, org: str, ns: str) -> Any:
    from server.llm.cache import CachedLLMClient

    try:
        return CachedLLMClient(
            llm, org=org, namespace=ns, key_scope=f"{org}/{ns}", deterministic_only=True
        )
    except Exception:  # noqa: BLE001 — a client the wrapper cannot take is used as is
        return llm


async def _remember(ctx: _AskCtx, res: AskResult, llm: Any) -> None:
    """Store a memoizable answer, or a negative entry for an abstain."""
    try:
        if res.abstain:
            if int(settings.code_ask_negative_cache_ttl_s) and ctx.index_head:
                await _store(
                    ctx,
                    _entry_row(
                        ctx,
                        kind="negative",
                        abstain_reason=res.abstain_reason[:2000],
                    ),
                )
            return
        if res.source != "synthesized" or not res.memoize or not res.answer:
            return
        cited = sorted({str(c["path"]) for c in res.citations})
        shown = [p for p in res.shown_paths if p not in cited][:_MAX_RETRIEVAL_PATHS]
        hashes = await _current_hashes(ctx.repo, ctx.org, ctx.ns, cited + shown)
        await _store(
            ctx,
            _entry_row(
                ctx,
                answer=res.answer,
                citations=json.dumps(res.citations),
                cited=(cited, [hashes.get(p, "") for p in cited]),
                shown=(shown, [hashes.get(p, "") for p in shown]),
                llm_model=str(getattr(llm, "model", "") or ""),
                confidence=res.retrieval.retrieval_confidence if res.retrieval else 0.0,
                cost_usd=max(0.0, _request_cost() - ctx.cost_at_start),
                latency_ms=int((time.monotonic() - ctx.started) * 1000),
            ),
        )
    except Exception as e:  # noqa: BLE001 — the answer is served whether or not it is memoized
        logger.warning("code.ask cache write failed: %s", e)


def _entry_row(
    ctx: _AskCtx,
    *,
    kind: str = "answer",
    answer: str = "",
    citations: str = "",
    cited: tuple[list[str], list[str]] = ([], []),
    shown: tuple[list[str], list[str]] = ([], []),
    llm_model: str = "",
    confidence: float = 0.0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    reused_from: str = "",
    abstain_reason: str = "",
) -> dict[str, Any]:
    digest = ctx.key.digest()
    return build_row(
        "code_answers",
        org=ctx.org,
        namespace=ctx.ns,
        question_id=entry_id(ctx.question, digest, kind),
        question=ctx.question[:2000],
        question_embedding=ctx.qvec,
        embedding_model=ctx.model,
        answer=answer,
        citations=citations,
        cited_paths=cited[0],
        cited_hashes=cited[1],
        llm_model=llm_model or ctx.key.synth_model,
        retrieval_confidence=confidence,
        indexed_commit=ctx.index_head[:40],
        cache_key=digest,
        prompt_version=ctx.key.prompt_version,
        entry_kind=kind,
        abstain_reason=abstain_reason,
        retrieval_paths=shown[0],
        retrieval_hashes=shown[1],
        cost_usd=cost_usd,
        latency_ms=max(0, min(latency_ms, 4_294_967_295)),
        reused_from=reused_from,
    )


async def _store(ctx: _AskCtx, row: dict[str, Any]) -> None:
    if not cache_namespace_ok(str(row.get("namespace") or "")) or not row.get("org"):
        logger.warning("code.ask refused a cache row with no resolved namespace")
        return
    await ctx.repo.insert_rows("code_answers", [row])


def _fixed(v: Any) -> str:
    return v.decode("utf-8", "replace").rstrip("\x00") if isinstance(v, bytes) else str(v or "")


def _paths_hashes(row: dict[str, Any], prefix: str) -> tuple[list[str], list[str]]:
    return (
        [str(p) for p in row.get(f"{prefix}_paths") or []],
        [_fixed(h) for h in row.get(f"{prefix}_hashes") or []],
    )


async def _files_current(repo: CodeRepo, org: str, ns: str, entry: dict[str, Any]) -> bool:
    """Every cited and every shown file of the entry still hashes as it did
    when the entry was written — one keyed read of `code_files` for both."""
    cited, cited_h = _paths_hashes(entry, "cited")
    shown, shown_h = _paths_hashes(entry, "retrieval")
    paths, stored = cited + shown, cited_h + shown_h
    if not paths:
        return True
    current = await _current_hashes(repo, org, ns, list(dict.fromkeys(paths)))
    return all(current.get(p, "") == h for p, h in zip(paths, stored, strict=False))


async def _current_hashes(repo: CodeRepo, org: str, ns: str, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    rows = await repo.file_hash_rows(org, ns, paths)
    return {str(r["path"]): _fixed(r["content_hash"]) for r in rows}


async def _tombstone_entry(repo: CodeRepo, entry: dict[str, Any]) -> None:
    """Tombstone one cached entry. The row must carry its own org and namespace: a
    partial projection would mirror into ('', ''), writing phantom rows while the stale
    entry itself stayed live."""
    try:
        if not entry.get("org") or not cache_namespace_ok(str(entry.get("namespace") or "")):
            logger.warning("code.ask refused to tombstone an entry with no namespace")
            return
        row = CodeRepo.tombstone_of("code_answers", entry)
        await repo.insert_rows("code_answers", [row])
    except Exception as e:  # noqa: BLE001 — invalidation is best-effort
        logger.debug("code answer tombstone failed: %s", e)


async def tombstone_served_entry(repo: CodeRepo, org: str, entry: dict[str, Any]) -> int:
    """A negative `code.outcome` on a cached `code.ask` answer tombstones the entry it
    was served from (and, for a branch copy, the trunk entry it was copied from).
    `entry` is the trace's `cache_entry` block. Returns how many entries were
    tombstoned."""
    targets = [
        (str(entry.get("namespace") or ""), str(entry.get("question_id") or "")),
        (str(entry.get("trunk_namespace") or ""), str(entry.get("trunk_question_id") or "")),
    ]
    n = 0
    for ns, qid in targets:
        if not cache_namespace_ok(ns) or not qid:
            continue
        try:
            qid = str(uuid.UUID(qid))
        except ValueError:
            continue
        rows = await repo.live_answer_entries(org, ns, qid)
        for row in rows or []:
            await _tombstone_entry(repo, dict(row))
            n += 1
    return n
