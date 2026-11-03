"""Pluggable per-namespace reranker, shared by memory.search, code.search and
knowledge.search.

When a namespace sets `rerank_endpoint` (or a request sets `rerank`),
`memory.search` POSTs its candidate pool to the endpoint of that NAME and orders
results by the response; code.search and knowledge.search post their fused heads
the same way. Rows an exact identifier match pins keep their place
and are not sent; with fewer than two rows left, no call is made. Names map to
URLs only in the operator's LOOM_RERANK_ENDPOINTS_JSON; a caller never supplies
the URL, and a value that is not a configured name (a URL included) reranks
nothing. Loom owns the data plane, not the model.

Built-in rerankers (no separate service to run): `builtin:openai` (or
`builtin:openai:gpt-4o-mini`) routes through Loom's LLM client;
`builtin:cohere` (or `builtin:cohere:rerank-english-v3.0`) calls Cohere.

Contract — request body:
{
  "query": "<original query string>",
  "prompt_template": "<the namespace's prompt_template, if any>",
  "candidates": [
    {"memory_key": "k", "namespace": "ns", "content_excerpt": "...",
     "memory_type": "preference", "similarity": 0.82},
    ...
  ]
}

Contract — response body:
{
  "ranked": [
    {"memory_key": "k", "namespace": "ns", "rank_score": 0.91},
    ...
  ]
}

Order of `ranked` is the new order; entries omitted from `ranked` get
appended after in their original order so a flaky reranker can't
silently drop hits. A key can name several candidates (one per session): each
entry naming it takes that key's next candidate in input order.

If the endpoint times out / errors / returns malformed JSON, the
candidate pool is returned in its original order — fail-open. Rerank
is a quality knob, not a correctness gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from server.clients import _http
from server.core.llm_free import reads_llm_free
from server.llm import customer
from server.llm.json_text import strip_code_fence
from server.memory.app.log_once import LogOnce
from server.memory.domain.rerank_names import BUILTIN_OPENAI, is_rerank_choice
from server.observability import degradations as _degradations
from server.observability.genai import llm_stage
from server.settings import BUILTIN_COHERE, is_builtin_cohere, settings

log = logging.getLogger(__name__)


def _timeout() -> httpx.Timeout:
    # not a setting: the connect, write and pool waits; the read is the setting
    return httpx.Timeout(
        connect=2.0, read=settings.memory_search_rerank_timeout_s, write=2.0, pool=2.0
    )


async def _post(url: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    """A reranker call on the model client, not followed elsewhere: its hook refuses a
    host off the gateway, sends the identity headers only to the gateway's hosts, and
    reads a gateway's refusal of the call's identity (`server.clients._http`)."""
    return await _http.get_model_client().post(
        url, json=body, headers=headers, timeout=_timeout(), follow_redirects=False
    )


_BUILTIN_COHERE_DEFAULT_MODEL = "rerank-english-v3.0"
_COHERE_API_URL = "https://api.cohere.com/v2/rerank"

_RERANK_SYSTEM_PROMPT = """Re-rank memory excerpts by how well they answer a query.

Input: a query and N candidate memories.
Output: a JSON object with one key, "ranked", whose value is the
candidates' integer indices in order from MOST to LEAST relevant.

Rules:
- Include EVERY candidate index. A memory that looks unrelated at a
  glance may answer the query in vocabulary the query doesn't share.
  Demote weak matches to the end of the list; dropping them is
  recall-destroying.
- If N == 0, return {"ranked": []}.
- If two candidates are equally relevant, either order is acceptable.

Output ONLY the JSON object. No commentary, no markdown.

Example (anchors the demote-not-drop rule):
  Query: "what is the user's preferred IDE?"
  Candidates:
    [0] User's IDE is VSCode.
    [1] User has a dog named Bowie.
    [2] User uses VSCode with the Catppuccin theme.
  Output: {"ranked": [0, 2, 1]}   (all three; [1] demoted, not dropped)"""


@dataclass(frozen=True)
class RerankCandidate:
    memory_key: str
    namespace: str
    content_excerpt: str
    memory_type: str
    similarity: float


# Names already logged as not configured, so a namespace holding one logs once per
# process, not once per search.
_UNCONFIGURED_LOGGED = LogOnce()
# Namespaces whose stored builtin:cohere was unset, logged once per process.
_COHERE_UNSET_LOGGED = LogOnce()


def _note_unconfigured(endpoint: str) -> None:
    """Log once that `endpoint` is not a configured name; nothing is posted to it."""
    shown = f"a URL on host {_host_of(endpoint)!r}" if "://" in endpoint else repr(endpoint[:64])
    if not _UNCONFIGURED_LOGGED.first(shown):
        return
    log.warning(
        "rerank endpoint %s is not a name in LOOM_RERANK_ENDPOINTS_JSON; not reranking", shown
    )


def _host_of(url: str) -> str:
    """Bounded span attribute: the reranker's host, never its full URL."""
    try:
        from urllib.parse import urlsplit

        return (urlsplit(url).hostname or "")[:64]
    except Exception:  # noqa: BLE001 — telemetry never raises
        return ""


async def rerank(
    *,
    endpoint: str,
    prompt_template: str,
    query: str,
    candidates: list[RerankCandidate],
    excerpt_chars: int = 300,
) -> list[RerankCandidate]:
    """Call the operator's reranker. Fail-open on every error path.

    Returns the candidates re-ordered by the reranker's response; entries
    missing from the response are appended in input order.
    """
    if not endpoint or endpoint == "none" or not candidates:
        return candidates

    if endpoint.startswith(BUILTIN_OPENAI):
        if reads_llm_free():
            return candidates  # an LLM reranker; LLM-free reads keep the fused order
        return await _rerank_with_builtin_openai(
            endpoint,
            query,
            candidates,
            excerpt_chars=excerpt_chars,
        )

    if endpoint.startswith(BUILTIN_COHERE):
        return await _rerank_with_builtin_cohere(endpoint, query, candidates)

    url = settings.rerank_endpoint_map().get(endpoint)
    if not url:
        _note_unconfigured(endpoint)
        return candidates

    body: dict[str, Any] = {
        "query": query,
        "prompt_template": prompt_template,
        "candidates": [
            {
                "memory_key": c.memory_key,
                "namespace": c.namespace,
                "content_excerpt": c.content_excerpt,
                "memory_type": c.memory_type,
                "similarity": c.similarity,
            }
            for c in candidates
        ],
    }
    # The operator endpoint is a plain HTTP hop with no LLM client behind it,
    # so nothing else would put it in the waterfall; without this span a slow
    # reranker read as slow retrieval.
    from server.observability.otel import traced

    # Outside the fail-open: a refused call is an error, not a kept order.
    headers = customer.headers(await customer.require())
    try:
        with traced("rerank.http", candidates=len(candidates), endpoint_host=_host_of(url)):
            resp = await _post(url, body, headers)
            resp.raise_for_status()
            payload = resp.json()
    except customer.HOLDS:
        raise  # the gateway refused or held the call's identity: never "kept the input order"
    except Exception as e:  # noqa: BLE001 — a failed reranker keeps the input order
        log.warning("rerank.call failed (%s); falling back to input order", e)
        _degradations.note("rerank", "input_order_kept", e)
        return candidates

    ranked = payload.get("ranked") if isinstance(payload, dict) else None
    if not isinstance(ranked, list):
        log.warning("rerank.call returned malformed payload; falling back")
        _degradations.note("rerank", "input_order_kept", "malformed payload")
        return candidates

    # The endpoint names a candidate by (namespace, key), and a key can name several (one per
    # session): each entry takes the key's next candidate in input order.
    by_key: dict[tuple[str, str], list[RerankCandidate]] = {}
    for c in candidates:
        by_key.setdefault((c.namespace, c.memory_key), []).append(c)
    ordered: list[RerankCandidate] = []
    for entry in ranked:
        if not isinstance(entry, dict):
            continue
        ns = entry.get("namespace") or ""
        key = entry.get("memory_key")
        if not key:
            continue
        queue = by_key.get((ns, key))
        if queue:
            ordered.append(queue.pop(0))
    taken = {id(c) for c in ordered}
    ordered.extend(c for c in candidates if id(c) not in taken)
    return ordered


async def _rerank_with_builtin_cohere(
    endpoint: str,
    query: str,
    candidates: list[RerankCandidate],
) -> list[RerankCandidate]:
    """Cohere rerank via their hosted API, with COHERE_API_KEY.

    Fail-open: any error returns candidates in input order.
    """
    from server.core.provider_keys import rerank_key

    api_key = rerank_key("cohere")
    if not api_key:
        log.warning(
            "builtin:cohere rerank requested but COHERE_API_KEY unset; falling back to input order",
        )
        return candidates

    # Optional model override: builtin:cohere:rerank-english-v3.0
    model = endpoint.split(":", 2)[2] if endpoint.count(":") >= 2 else _BUILTIN_COHERE_DEFAULT_MODEL

    body = {
        "model": model,
        "query": query,
        "documents": [c.content_excerpt[:1024] for c in candidates],
        "top_n": len(candidates),
        "return_documents": False,
    }
    from server.observability.otel import traced

    who = await customer.require()  # outside the fail-open: a refusal is an error, not a kept order
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **customer.headers(who),
    }
    try:
        with traced("rerank.cohere", model=model, candidates=len(candidates)):
            resp = await _post(_COHERE_API_URL, body, headers)
            resp.raise_for_status()
            payload = resp.json()
    except customer.HOLDS:
        raise  # never "kept the input order"
    except Exception as e:  # noqa: BLE001 — a failed Cohere call keeps the input order
        log.warning("Cohere rerank failed: %s; falling back to input order", e)
        _degradations.note("rerank:cohere", "input_order_kept", e)
        return candidates

    results = payload.get("results", [])
    if not isinstance(results, list):
        return candidates

    ordered: list[RerankCandidate] = []
    seen_idx: set[int] = set()
    for r in results:
        if not isinstance(r, dict):
            continue
        idx = r.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
            continue
        if idx in seen_idx:
            continue
        ordered.append(candidates[idx])
        seen_idx.add(idx)
    for i, c in enumerate(candidates):
        if i not in seen_idx:
            ordered.append(c)
    return ordered


async def _rerank_with_builtin_openai(
    endpoint: str,
    query: str,
    candidates: list[RerankCandidate],
    *,
    excerpt_chars: int = 300,
) -> list[RerankCandidate]:
    """Built-in LLM reranker via Loom's existing LLM client.

    Falls back to input order on any failure — never silently drops a hit.
    """
    import json as _json
    import re as _re

    from server.llm.client import get_llm_client, get_llm_client_for_model
    from server.llm.fake import FakeLLMClient

    # Optional model override: builtin:openai:gpt-4o-mini
    model_override = ""
    if endpoint.count(":") >= 2:
        model_override = endpoint.split(":", 2)[2]

    candidate_lines = "\n".join(
        f"  [{i}] {c.content_excerpt[:excerpt_chars]}" for i, c in enumerate(candidates)
    )
    user_prompt = (
        f"Query: {query}\n\n"
        f"Candidates:\n{candidate_lines}\n\n"
        'Return the JSON {"ranked": [...]} object now.'
    )
    # Output is one index per candidate (~6 tokens); scale the budget to the
    # pool so a fixed cap can't truncate the ranking and pin deep hits.
    rerank_max_tokens = min(4096, max(256, len(candidates) * 6 + 64))

    try:
        client = get_llm_client()
        # FakeLLMClient can't rerank — fail-open so unit tests don't need an LLM.
        if isinstance(client, FakeLLMClient):
            return candidates
        # A per-model client, never a mutated shared one: `client` comes from a
        # process-wide cache, and concurrent requests with different overrides
        # would observe each other's model (cost accumulator and
        # `gen_ai.request.model` mis-attributed). get_llm_client_for_model
        # keeps a separate cache entry per (provider, model).
        if model_override:
            client = get_llm_client_for_model(model_override)
        with llm_stage("inline_rerank"):
            result = await client.complete(
                system=_RERANK_SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=rerank_max_tokens,
            )
    except customer.HOLDS:
        raise  # a refusal or an unread switch: never "kept the input order"
    except Exception as e:  # noqa: BLE001 — a failed LLM rerank keeps the input order
        log.warning("builtin:openai rerank failed (%s); fallback", e)
        _degradations.note("rerank:openai", "input_order_kept", e)
        return candidates

    try:
        text = strip_code_fence(result.text)
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object")
        payload = _json.loads(text[start : end + 1])
        ranked = payload.get("ranked")
        if not isinstance(ranked, list):
            raise ValueError("ranked must be a list")
        indices: list[int] = []
        for item in ranked:
            if isinstance(item, int):
                indices.append(item)
            elif isinstance(item, str) and _re.fullmatch(r"\d+", item):
                indices.append(int(item))
    except (ValueError, _json.JSONDecodeError) as e:
        log.warning("builtin:openai rerank parse failed (%s); fallback", e)
        _degradations.note("rerank:openai", "input_order_kept", f"parse: {e}")
        return candidates

    # Apply the ranking — kept indices first (in LLM order), dropped
    # ones appended after (so we never lose a hit).
    n = len(candidates)
    seen_ix: set[int] = set()
    ordered: list[RerankCandidate] = []
    for ix in indices:
        if 0 <= ix < n and ix not in seen_ix:
            ordered.append(candidates[ix])
            seen_ix.add(ix)
    for i, c in enumerate(candidates):
        if i not in seen_ix:
            ordered.append(c)
    return ordered


async def get_namespace_rerank_config(
    org: str,
    namespace: str,
) -> tuple[str, str]:
    """Look up (rerank_endpoint, rerank_prompt_template) for a namespace.

    Returns ("", "") when not configured.
    """
    from server.core.ch import get_client

    try:
        row = await get_client().query_one(
            """
            SELECT
                ifNull(rerank_endpoint, '') AS rerank_endpoint,
                ifNull(rerank_prompt_template, '') AS rerank_prompt_template
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace},
        )
    except Exception as e:  # noqa: BLE001 — a failed config read means no reranker
        log.warning("rerank config lookup failed: %s", e)
        return "", ""
    if not row:
        return "", ""
    endpoint = str(row.get("rerank_endpoint") or "").strip()
    if not is_rerank_choice(endpoint):
        # a row written before endpoints were names (a raw URL): never posted to
        _note_unconfigured(endpoint)
        return "", ""
    if endpoint == "none":
        return "", ""
    if settings.gateway_required() and is_builtin_cohere(endpoint):
        # Stored before the gateway was required: Cohere's own API would be
        # refused at each search, unseen behind the fail-open, so it is unset.
        if _COHERE_UNSET_LOGGED.first((org, namespace)):
            log.warning(
                "%s/%s reranks with builtin:cohere, refused while %s is set: not reranked",
                org,
                namespace,
                settings.gateway_required_by(),
            )
        return "", ""
    return (endpoint, row.get("rerank_prompt_template") or "")
