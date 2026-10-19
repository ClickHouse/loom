"""LLM-backed query helpers for memory search.

The iterative relevance judge, derived-intent mapping, triple extraction,
and verb-synonym expansion. The LLM client +
extraction preamble are imported lazily inside each fn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from server.llm.json_text import strip_code_fence
from server.memory.domain.graph_quality import canonical_entity_text
from server.memory.domain.search_prompts import (
    _DERIVED_INTENT_PROMPT,
    _ITERATIVE_JUDGE_PROMPT,
    _LLM_TRIPLE_PROMPT,
    _VERB_SYNONYM_PROMPT,
)
from server.memory.domain.search_text import canonicalize_verb
from server.observability.genai import staged
from server.settings import settings
from server.settings.budgets import llm_max_tokens


@staged("iterative_judge")
async def judge_results_iteratively(
    query: str,
    excerpts: list[str],
    *,
    timeout_s: float = 6.0,
) -> tuple[bool, str]:
    """LLM-judge: do the retrieved excerpts answer the query? If not,
    suggest one alternate query for a second-pass retrieval.

    Returns (answered, suggested_query). Fails open: any error returns
    (True, "") so the caller defaults to trusting the original retrieval.
    """
    if not query.strip() or not excerpts:
        return True, ""
    try:
        from server.llm.client import get_llm_client_for_model
        from server.memory.app.extract import with_strict_examples_preamble

        llm = get_llm_client_for_model(settings.llm_model)
        joined = "\n".join(f"  [{i}] {x[:200]}" for i, x in enumerate(excerpts[:6]))
        user_msg = f"Query: {query}\n\nExcerpts:\n{joined}"
        result = await asyncio.wait_for(
            llm.complete(
                with_strict_examples_preamble(_ITERATIVE_JUDGE_PROMPT),
                user_msg,
                max_tokens=llm_max_tokens("search_judge"),
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        return True, ""
    except Exception:  # noqa: BLE001 — a failed judge call counts as answered
        return True, ""
    raw = strip_code_fence(result.text)
    try:
        import json as _json

        obj = _json.loads(raw)
    except ValueError:
        return True, ""
    answered = bool(obj.get("answered", True))
    suggested = obj.get("suggested_query", "")
    suggested = suggested.strip() if isinstance(suggested, str) else ""
    return answered, suggested


@staged("derived_intent")
async def extract_derived_intent_llm(
    query: str,
    *,
    timeout_s: float = 6.0,
) -> dict[str, str] | None:
    """Map a derived-answer query to a structured aggregate intent over
    loom.derived_facts.

    Returns {op, subject, predicate, category, object} with op ∈
    {"count","sum","date_span"}, or None when the query is not a derived
    aggregate (op == "") or on any LLM error. Fail-open: callers treat
    None as "no aggregate leg, use the vector answer path".
    """
    if not query.strip():
        return None
    try:
        from server.llm.client import get_llm_client_for_model
        from server.memory.app.extract import with_strict_examples_preamble

        llm = get_llm_client_for_model(settings.llm_model)
        result = await asyncio.wait_for(
            llm.complete(
                with_strict_examples_preamble(_DERIVED_INTENT_PROMPT),
                f"query: {query}",
                max_tokens=llm_max_tokens("search_derived_intent"),
            ),
            timeout=timeout_s,
        )
    except (TimeoutError, Exception):  # noqa: BLE001 — a failed intent call means no derived intent
        return None

    raw = strip_code_fence(result.text)
    try:
        import json as _json

        obj = _json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    op = str(obj.get("op", "")).strip().lower()
    if op not in ("count", "sum", "date_span"):
        return None
    return {
        "op": op,
        "subject": str(obj.get("subject") or "user").strip().lower() or "user",
        "predicate": str(obj.get("predicate") or "").strip().lower(),
        "category": str(obj.get("category") or "").strip().lower(),
        "object": str(obj.get("object") or "").strip().lower(),
    }


_QUERY_ENTITY_CACHE: dict[str, list[list[str]]] = {}


_QUERY_ENTITY_CACHE_MAX = 2048


@staged("triple_extract")
async def extract_query_triples_llm(
    query: str,
    *,
    timeout_s: float = 3.0,
    on_llm_call: Callable[[int], None] | None = None,
) -> list[tuple[str, str, str]]:
    """LLM-extracted (s, v, o) triples from a query. Used to look up
    matching stored triples in loom.entity_triples for the triple-boost
    leg of graph mode. Cached per-query, fail-open ([] on error).
    Wildcard '?' permitted for unknown subject or object — the matcher
    treats it as a SQL LIKE %% on that field.

    `on_llm_call` fires only when a real completion is issued (cache
    hits cost nothing).
    """
    if not query or not query.strip():
        return []
    key = "TRIPLES:" + query.strip().lower()
    cached = _QUERY_ENTITY_CACHE.get(key)
    if cached is not None:
        return [tuple(t) for t in cached]  # type: ignore[misc]
    try:
        from server.llm.client import get_llm_client
        from server.memory.app.extract import with_strict_examples_preamble

        client = get_llm_client()
        if on_llm_call is not None:
            on_llm_call(1)
        result = await asyncio.wait_for(
            client.complete(
                with_strict_examples_preamble(_LLM_TRIPLE_PROMPT),
                query,
                max_tokens=llm_max_tokens("search_query_triples"),
            ),
            timeout=timeout_s,
        )
        raw = strip_code_fence(result.text)
        import json as _json

        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            return []
        data = _json.loads(raw[start : end + 1])
        out: list[tuple[str, str, str]] = []
        for item in data[:6] if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            s = canonical_entity_text(str(item.get("s", "")))
            v = canonicalize_verb(str(item.get("v", "")))
            o = canonical_entity_text(str(item.get("o", ""))) or "?"
            if not v or len(v) < 2:
                continue
            if not s and not o:
                continue
            out.append((s or "?", v, o or "?"))
        if len(_QUERY_ENTITY_CACHE) >= _QUERY_ENTITY_CACHE_MAX:
            _QUERY_ENTITY_CACHE.pop(next(iter(_QUERY_ENTITY_CACHE)))
        _QUERY_ENTITY_CACHE[key] = [list(t) for t in out]
        return out
    except Exception:  # noqa: BLE001 — a failed entity extraction means no query entities
        return []


_VERB_SYNONYM_CACHE: dict[str, list[str]] = {}


_VERB_SYNONYM_CACHE_MAX = 10000


@staged("verb_synonyms")
async def expand_verb_synonyms(
    verb: str,
    *,
    timeout_s: float = 3.0,
    on_llm_call: Callable[[int], None] | None = None,
) -> list[str]:
    """LLM-expand a verb into its synonym set. Returns the input verb +
    3-5 synonyms, all canonicalized (stemmed + lowercased + underscore-
    joined). Cached process-wide by canonicalized form.

    Fail-open: returns [verb] on any error so the matcher still works.
    Used at query time to widen the relation filter in
    fetch_triple_matched_memories.

    `on_llm_call` fires only on a real completion (cache hits cost
    nothing).
    """
    canon = canonicalize_verb(verb)
    if not canon:
        return []
    cached = _VERB_SYNONYM_CACHE.get(canon)
    if cached is not None:
        return list(cached)
    try:
        from server.llm.client import get_llm_client
        from server.memory.app.extract import with_strict_examples_preamble

        client = get_llm_client()
        if on_llm_call is not None:
            on_llm_call(1)
        result = await asyncio.wait_for(
            client.complete(
                with_strict_examples_preamble(_VERB_SYNONYM_PROMPT),
                f'verb: "{canon}"',
                max_tokens=llm_max_tokens("search_verb_synonyms"),
            ),
            timeout=timeout_s,
        )
        raw = strip_code_fence(result.text)
        import json as _json

        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            return [canon]
        data = _json.loads(raw[start : end + 1])
        out: list[str] = [canon]
        seen: set[str] = {canon}
        for v in data[:6] if isinstance(data, list) else []:
            cv = canonicalize_verb(str(v))
            if cv and cv not in seen and len(cv) >= 2:
                seen.add(cv)
                out.append(cv)
        if len(_VERB_SYNONYM_CACHE) >= _VERB_SYNONYM_CACHE_MAX:
            _VERB_SYNONYM_CACHE.pop(next(iter(_VERB_SYNONYM_CACHE)))
        _VERB_SYNONYM_CACHE[canon] = out
        return out
    except Exception:  # noqa: BLE001 — a failed synonym call keeps the verb alone
        return [canon]
