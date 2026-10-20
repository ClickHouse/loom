"""The card leg and the concept leg. The nearest file cards vote for their
files; the concept leg runs a lexical pass on the vocabulary the code likely
uses, named by a model or read off the nearest cards' files, which shares the
card leg's read when both want source cards."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from server.code.app.search.lexical import _lexical_terms
from server.code.app.search.state import _POOL
from server.code.app.spans import code_span, span_attrs
from server.code.domain.index import query_trim
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo
from server.settings import settings
from server.settings.budgets import llm_max_tokens

logger = logging.getLogger(__name__)


async def _card_leg_rows(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    *,
    query_vec: Any,
    path_prefix: str,
    use_cards: bool,
    gate_only: bool,
    work: dict[str, Any],
    card_stats: dict[str, Any],
    cards: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The card leg: the nearest file cards vote for their files, whose skeleton chunks
    return as rows for the fusion; the top card's cosine is kept for the off_domain
    gate. With `gate_only` three cards are read and no chunks. `cards`: the leg's card
    rows already read by the caller, in distance order — no second read."""
    if not (use_cards or gate_only):
        return []
    rows: list[dict[str, Any]] = []
    # In `best_chunk` mode the leg reads source cards only and brings each file's chunk
    # nearest the query, not its skeleton
    best_chunk = _card_leg_best_chunk(use_cards)
    with code_span("search.card_leg") as sp:
        try:
            cvec = await query_vec()
            if cards is None:
                work["card_queries"] = work.get("card_queries", 0) + 1
                cards = await repo.vector_search_cards(
                    org,
                    ns,
                    model,
                    cvec,
                    path_prefix=path_prefix,
                    pool=_card_leg_pool(use_cards),
                    skip_tests=best_chunk,
                )
            else:
                cards = cards[: _card_leg_pool(use_cards)]
            if cards and cards[0].get("distance") is not None:
                card_stats["top_similarity"] = 1.0 - float(cards[0]["distance"])
                # retrieval_work holds ints: thousandths of the cosine
                work["card_top_similarity_milli"] = int(round(card_stats["top_similarity"] * 1000))
            card_stats["paths"] = [str(c["path"]) for c in cards if c.get("path")]
            paths = card_stats["paths"] if use_cards else []
            card_stats["files"] = len(paths)
            if paths and best_chunk:
                rows = await repo.best_chunks_for_paths(
                    org,
                    ns,
                    model,
                    cvec,
                    paths,
                    per_path=int(settings.code_search_card_leg_units_per_file),
                )
            elif paths:
                order = {f: i for i, f in enumerate(paths)}
                rows = await repo.chunks_for_paths(
                    org,
                    ns,
                    model,
                    paths,
                    per_path=int(settings.code_search_card_leg_units_per_file),
                )
                rows.sort(
                    key=lambda r: (
                        order.get(str(r.get("path")), 1 << 20),
                        int(r.get("start_line") or 0),
                    )
                )
        except Exception as e:  # noqa: BLE001 — a vote, never a gate
            logger.warning("code.search card leg failed: %s", e)
            work["card_leg_failed"] = 1
            rows = []
        span_attrs(sp, results=len(rows), files=card_stats["files"])
    return rows


async def _concept_leg_rows(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    *,
    query: str,
    concept_model: str,
    path_prefix: str,
    scope: SearchScope | None,
    work: dict[str, Any],
    concept_stats: dict[str, Any],
    terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The concept expansion leg: the identifiers and vocabulary the code likely uses
    for a prose question — named by a language model, or handed in as `terms` (the
    symbols of the files the nearest cards point at) — and a lexical pass on the rare
    ones returns rows for the fusion. Empty `concept_model` and no terms = off."""
    if not concept_model and not terms:
        return []
    rows: list[dict[str, Any]] = []
    work["concept_queries"] = work.get("concept_queries", 0) + 1
    with code_span("search.concept_leg") as sp:
        try:
            t0 = time.perf_counter()
            if terms is None:
                from server.llm.cache import wrap_scoped
                from server.llm.client import get_llm_client_for_model
                from server.observability.genai import llm_stage

                # Behind the exact-prompt cache inside code.ask
                llm = wrap_scoped(get_llm_client_for_model(concept_model))
                with llm_stage("code_query_expand"):  # the ledger's series for code.ask's expansion
                    res = await llm.complete(
                        _CONCEPT_SYSTEM.format(namespace=ns),
                        query,
                        max_tokens=llm_max_tokens("code_query_expand"),
                        response_format={"type": "json_object"},
                    )
                terms = parse_concept_terms(
                    getattr(res, "text", "") or "",
                    max_terms=int(settings.code_search_concept_expand_max_terms),
                )
            work["concept_ms"] = int((time.perf_counter() - t0) * 1000)
            work["concept_terms"] = len(terms)
            concept_stats["terms"] = terms
            if terms:
                plan2 = query_trim.plan(
                    " ".join(terms), None, settings.code_search_query_max_tokens, {}
                )
                c_tokens, c_idfs = await _lexical_terms(
                    repo,
                    org,
                    ns,
                    plan2.tokens,
                    work,
                    plan2,
                )
                # a generic token ("Query", "Settings") would turn the pass
                # into a scan of half the corpus: keep the rare ones
                floor = float(settings.code_search_concept_expand_min_idf)
                kept = [(t, i) for t, i in zip(c_tokens, c_idfs, strict=True) if i >= floor]
                work["concept_tokens_dropped"] = len(c_tokens) - len(kept)
                c_tokens = [t for t, _ in kept]
                c_idfs = [i for _, i in kept]
                if c_tokens:
                    rows = await repo.lexical_search_chunks(
                        org,
                        ns,
                        model,
                        c_tokens,
                        c_idfs,
                        path_prefix=path_prefix,
                        pool=_POOL,
                        scope=scope,
                    )
        except Exception as e:  # noqa: BLE001 — a vote, never a gate
            logger.warning("code.search concept leg failed: %s", e)
            work["concept_leg_failed"] = 1
            rows = []
        span_attrs(sp, results=len(rows), terms=len(concept_stats["terms"]))
    return rows


async def _card_and_concept_rows(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    *,
    query: str,
    query_vec: Any,
    use_cards: bool,
    card_gate: float,
    concept_model: str,
    path_prefix: str,
    scope: SearchScope | None,
    work: dict[str, Any],
    card_stats: dict[str, Any],
    concept_stats: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The card leg and the concept leg, without the hypothetical-answer leg."""
    term_files = card_term_files(concept_model)
    if term_files > 0:
        # the nearest cards of a behaviour question are mostly test files (two thirds of
        # the ClickHouse cards are), whose symbols are statements: the term files come
        # from a source-only card read. When the card leg reads source cards too
        # (best_chunk mode), both legs share one read — the same vector, the same order,
        # the longer LIMIT.
        shared = _card_leg_best_chunk(use_cards)
        pool = max(term_files, _card_leg_pool(use_cards)) if shared else term_files

        async def _source_cards() -> list[dict[str, Any]]:
            try:
                work["card_queries"] = work.get("card_queries", 0) + 1
                return await repo.vector_search_cards(
                    org,
                    ns,
                    model,
                    await query_vec(),
                    path_prefix=path_prefix,
                    pool=pool,
                    skip_tests=True,
                )
            except Exception as e:  # noqa: BLE001 — a vote, never a gate
                logger.warning("code.search card term read failed: %s", e)
                return []

        # The concept terms need only the source cards, so the concept pass (symbol
        # names -> lexical read) runs beside the card leg's chunk read instead of after
        # it; the rows are the same.
        source = asyncio.ensure_future(_source_cards())

        async def _concepts() -> list[dict[str, Any]]:
            source_cards = await asyncio.shield(source)
            terms: list[str] = []
            paths = [str(c["path"]) for c in source_cards if c.get("path")][:term_files]
            work["concept_term_files"] = len(paths)
            if paths:
                try:
                    names = await repo.symbol_names_for_paths(org, ns, paths)
                except Exception as e:  # noqa: BLE001 — a vote, never a gate: no terms
                    logger.warning("code.search concept term read failed: %s", e)
                    work["concept_leg_failed"] = 1
                    names = []
                terms = select_card_terms(
                    names,
                    max_terms=int(settings.code_search_concept_expand_max_terms),
                )
            return await _concept_leg_rows(
                repo,
                org,
                ns,
                model,
                query=query,
                concept_model="",
                path_prefix=path_prefix,
                scope=scope,
                work=work,
                concept_stats=concept_stats,
                terms=terms,
            )

        async def _cards() -> list[dict[str, Any]]:
            if shared:
                work["card_read_shared"] = 1
            return await _card_leg_rows(
                repo,
                org,
                ns,
                model,
                query_vec=query_vec,
                path_prefix=path_prefix,
                use_cards=use_cards,
                gate_only=not shared and card_gate > 0.0 and not use_cards,
                work=work,
                card_stats=card_stats,
                cards=(await asyncio.shield(source)) if shared else None,
            )

        try:
            cards, concepts = await asyncio.gather(_cards(), _concepts())
        finally:
            if not source.done():
                source.cancel()
        return cards, concepts
    cards, concepts = await asyncio.gather(
        _card_leg_rows(
            repo,
            org,
            ns,
            model,
            query_vec=query_vec,
            path_prefix=path_prefix,
            use_cards=use_cards,
            gate_only=card_gate > 0.0 and not use_cards,
            work=work,
            card_stats=card_stats,
        ),
        _concept_leg_rows(
            repo,
            org,
            ns,
            model,
            query=query,
            concept_model=concept_model,
            path_prefix=path_prefix,
            scope=scope,
            work=work,
            concept_stats=concept_stats,
        ),
    )
    return cards, concepts


def _card_leg_best_chunk(use_cards: bool) -> bool:
    return use_cards and str(settings.code_search_card_leg_mode).strip() == "best_chunk"


def _card_leg_pool(use_cards: bool) -> int:
    return int(settings.code_search_card_leg_files) if use_cards else 3


def card_term_files(concept_model: str) -> int:
    """`cards` or `cards:<n>` → the number of nearest card files whose symbols feed the
    concept leg; 0 for a model name."""
    spec = (concept_model or "").strip().lower()
    if spec == "cards":
        return 5
    if spec.startswith("cards:") and spec[6:].isdigit():
        return max(1, int(spec[6:]))
    return 0


_TERM_KIND_RANK = {
    "class": 0,
    "struct": 0,
    "interface": 0,
    "enum": 1,
    "type": 1,
    "function": 2,
    "method": 2,
    "const": 3,
}


# SQL statements and test fixtures are not vocabulary a question would use
_TERM_SKIP_KINDS = frozenset({"statement", "fixture"})


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def select_card_terms(names: list[dict[str, Any]], *, max_terms: int = 8) -> list[str]:
    """The identifiers a card's files define, most telling first: types
    before functions, then by how many of the files define the name; names
    shorter than four characters, all-lowercase common words and duplicates
    (case-insensitive) are dropped."""
    seen: dict[str, tuple[int, int, str]] = {}
    for r in names:
        n = str(r.get("name") or "").strip()
        kind = str(r.get("kind") or "")
        if kind in _TERM_SKIP_KINDS or not _IDENT_RE.match(n):
            continue
        if len(n) < 4 or n.islower() and "_" not in n:
            continue
        key = n.lower()
        rank = _TERM_KIND_RANK.get(kind, 4)
        prev = seen.get(key)
        seen[key] = (min(rank, prev[0]) if prev else rank, (prev[1] if prev else 0) + 1, n)
    ordered = sorted(seen.values(), key=lambda t: (t[0], -t[1], t[2]))
    return [t[2] for t in ordered[:max_terms]]


_CONCEPT_SYSTEM = (
    "You map a developer's natural-language question about the code repository indexed "
    "as `{namespace}` to the vocabulary the code itself is likely to use. Answer with JSON "
    '{{"identifiers": [...], "terms": [...]}}: up to 6 class, function, file or setting '
    "names spelled as they would appear in the code (CamelCase or snake_case), and up to 4 "
    "short technical phrases (2-3 words). Only names you are confident exist in that "
    "repository; specific ones — never a generic word such as Query, Settings, Server, "
    "Context, Storage on its own; no explanations."
)


def parse_concept_terms(text: str, *, max_terms: int = 8) -> list[str]:
    """The model's JSON → a de-duplicated list of terms, identifiers first;
    tolerant of a fenced or prose-wrapped object, empty on anything else."""
    import json as _json

    raw = (text or "").strip()
    if "{" in raw and "}" in raw:
        raw = raw[raw.index("{") : raw.rindex("}") + 1]
    try:
        obj = _json.loads(raw)
    except ValueError:
        return []
    if not isinstance(obj, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for key in ("identifiers", "terms"):
        vals = obj.get(key) or []
        if not isinstance(vals, list):
            continue
        for v in vals:
            t = str(v).strip().strip("`'\"")
            if 1 < len(t) <= 64 and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
            if len(out) >= max_terms:
                return out
    return out
