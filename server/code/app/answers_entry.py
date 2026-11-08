"""A code.ask answer and what it is keyed by: the result record, the question's normal
form and ids, the prompts and their version, the cache key and the hit thresholds."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from server.code.app.search import CodeSearchResult
from server.code.store.index import CodeRepo
from server.settings import settings
from server.settings.budgets import llm_max_tokens

# The cache-hit cosine is per embedding model — a vector space's similarity scale is its
# own. The values below were measured WITH the identifier guard (`guard_passes`: a hit
# must name the same identifiers, settings, paths, numbers, quoted strings and error codes
# as the question) on the pairs in bench/code_ask_cache_threshold (42 paraphrase /
# 43 different) plus 22 near-miss pairs that differ only by one identifier. Cosine alone
# cannot separate those (issue #41777 vs #41778 sits at 0.9857: 0 of 42 paraphrases would
# survive the 0.99 it needs); the guard refuses all 22 and 12 of the 43.
# text-embedding-3-large: the closest different pair the guard lets through is 0.8344
# ("how does loom_search choose namespaces" / "... rank results across namespaces"); 0.84
# is the lowest value with no false hit (17 of 42), and 0.85 is taken instead: 14 of 42
# for a 0.0156 margin rather than 0.0056 on 65 pairs (0.88 unguarded: 12).
# text-embedding-3-small: that same pair is 0.8637, so it stays 0.87 (11 of 42 with the
# guard). A model not listed takes the strictest measured value: a threshold too high
# costs a cache miss, one too low serves a wrong answer. `LOOM_CODE_ASK_CACHE_THRESHOLD`
# (> 0) overrides all. The thresholds assume the guard; there is no switch that turns it
# off.
_CACHE_HIT_THRESHOLD = 0.87
CACHE_HIT_THRESHOLDS: dict[str, float] = {
    "text-embedding-3-small": 0.87,
    "text-embedding-3-large": 0.85,
}
_MAX_CONTEXT_UNITS = 8
# Mechanism expansion: extra units pulled by matching question terms
# against the symbol table of the retrieved files (see _expand_mechanism).
_MAX_EXPANSION_UNITS = 4


_SYNTH_SYSTEM = (
    "You answer questions about a codebase from provided code excerpts "
    "ONLY. Commit to the mechanism the excerpts show: name the exact "
    "classes/functions that implement it and walk the control flow. Cite "
    "every claim with [path:start-end] markers referencing the excerpts. "
    "Never contradict your own citations with boilerplate hedging; if one "
    "specific piece is genuinely absent, note it in one short sentence at "
    "the end. If the excerpts do not answer the question at all, say "
    "exactly what is missing instead of guessing. Be concise: a few short "
    "paragraphs, no preamble. End with EXACTLY one final line: "
    "'COVERAGE: full' if the excerpts fully answer the question, "
    "'COVERAGE: partial' if they answer part of it, or "
    "'COVERAGE: none' if they do not answer it."
)

_HISTORY_NOTE = (
    "\n\nRepository history — authoritative PR / issue records from the "
    "index (answer merge state, authorship and linked issues from these; "
    "cite as [history]):\n"
)
_MEMORY_NOTE = (
    "\n\nRelated project memory — decisions / rationale / prior incidents "
    "(context for WHY, not a code citation; do not cite as code):\n"
)
_EXCERPT_CHARS = 4000

# Registered in Langfuse as `loom-code-query-expand` (scripts/codegen/push_langfuse_prompts.py);
# a constant so each generation links to a prompt version.
# NONE must be exceptional and evidence-based: a loose "reply NONE
# if about a different technology" rule makes the model answer NONE
# for in-repository questions such as 'how does a part get selected
# for a background merge'. The corpus description must match what
# is actually indexed, or questions about the repo's own python/shell
# test and CI tooling read as foreign and abstain before retrieval.
_EXPAND_SYSTEM = (
    "You map questions about the ClickHouse repository to "
    "identifiers. The index covers the C++ engine (src/) AND the "
    "repo's own python/shell test and CI tooling (tests/, ci/): "
    "the pytest integration framework, clickhouse-test, docker "
    "compose helpers, praktika CI, and test services like "
    "zookeeper, minio, azurite. Reply with up to 6 identifiers "
    "(class or function names in any of those languages) most "
    "likely implementing the mechanism — space-separated "
    "identifiers only, no prose. Questions use plain English "
    "(merges, snapshots, fixtures, containers, CI jobs): ALWAYS "
    "propose identifiers for them. Reply NONE only when the "
    "question EXPLICITLY NAMES a product, framework, or language "
    "with no presence in this repository (postgres, react, "
    "golang, redis, django, spring, terraform, v8, swift) — "
    "never infer foreignness from topic alone; docker, pytest, "
    "zookeeper, and minio ARE part of this repository's tooling."
)


@dataclass
class AskResult:
    answer: str = ""
    citations: list[dict[str, Any]] = dc_field(default_factory=list)
    source: str = "synthesized"  # cached | synthesized | abstained
    cache_similarity: float = 0.0
    abstain: bool = False
    abstain_reason: str = ""
    retrieval: CodeSearchResult | None = None
    tokens_returned: int = 0
    # Synthesizer's COVERAGE self-grade (full|partial|none); "" for cached
    # hits (grade belongs to the original synthesis) and abstains.
    coverage: str = ""
    # What the answer cache did — hit | miss | stale | guarded | key_mismatch | negative
    # | reused_from:<ns> | bypassed — and, on a served entry, the synthesis wall and
    # dollars it saved plus the entry's address (a negative code.outcome tombstones it).
    cache: str = "miss"
    cache_saved_ms: int = 0
    cache_saved_usd: float = 0.0
    cache_entry: dict[str, str] = dc_field(default_factory=dict)
    # internal to the write path: whether a synthesized answer may be
    # memoized, and the files its synthesis was shown
    memoize: bool = False
    shown_paths: list[str] = dc_field(default_factory=list)
    # the memory namespace and keys of the excerpts the synthesis quoted, stored with
    # the entry so an erasure of that memory finds it
    memory_namespace: str = ""
    memory_keys: list[str] = dc_field(default_factory=list)


def question_id(question: str) -> str:
    normalized = " ".join(question.lower().split())
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"loom://code-answer/{normalized}"))


def normalize_question(question: str) -> str:
    """The exact-match form of a question: case and whitespace folded, the
    trailing punctuation dropped (the negative cache's identity)."""
    return " ".join((question or "").lower().split()).rstrip(" ?.!")


def entry_id(question: str, key_digest: str, kind: str = "answer") -> str:
    """The RMT key of an entry. The cache key rides inside it, so two asks of one
    question under different keys (a budget, a scope) are two rows instead of one row
    the later write overwrites, and a negative entry never replaces the answer entry of
    its question."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"loom://code-answer/{kind}/{key_digest}/{normalize_question(question)}",
        )
    )


def cache_namespace_ok(ns: str) -> bool:
    """The cache is per code namespace; an ask that did not resolve one (`''`, or the
    identity tuple's `-` placeholder) neither reads nor writes it — such rows would be
    served to every other unresolved ask."""
    return (ns or "").strip() not in ("", "-")


@dataclass
class _AskCtx:
    repo: CodeRepo
    org: str
    ns: str
    question: str
    qvec: list[float]
    model: str
    key: CacheKey
    index_head: str
    started: float
    cost_at_start: float


def cache_hit_threshold(model: str) -> float:
    """The cosine a cached question must reach for `model`: the settings override when
    set, else the per-model table, else the strictest measured value."""
    override = float(settings.code_ask_cache_threshold)
    if override > 0:
        return override
    return CACHE_HIT_THRESHOLDS.get((model or "").strip(), _CACHE_HIT_THRESHOLD)


# ---- the answer cache -----------------------------------------------------

# Bump when the cache gate or the answer policy changes in a way the prompt
# text does not show (what is memoized, what is cited).
_ANSWER_POLICY = (
    "policy-2: coverage-none and non-public memory context are never memoized; memory "
    "excerpts come from the bound namespace alone and are stamped on the entry"
)


# The token budgets an entry is keyed on: a budget within a bucket retrieves
# the same excerpts up to the tail, one past it may not.
_BUDGET_BUCKETS = (1000, 2000, 4000, 8000, 12000)
# The search arguments that change what the synthesis is shown.
_SCOPE_ARGS = ("path_prefix", "path_globs", "exclude_globs", "langs", "pattern", "top_k")


def prompt_version() -> str:
    """sha256 (16 hex) of everything that turns the same excerpts into a
    different answer: the synthesis and expansion prompts, the notes the
    synthesis prompt may carry, the context bounds and the answer policy.
    An entry written under another version is stale — a prompt change
    invalidates the cache with no manual purge."""
    parts = [
        _SYNTH_SYSTEM,
        _EXPAND_SYSTEM,
        _HISTORY_NOTE,
        _MEMORY_NOTE,
        _MAX_CONTEXT_UNITS,
        _MAX_EXPANSION_UNITS,
        _EXCERPT_CHARS,
        llm_max_tokens("code_answer_synth"),
        _ANSWER_POLICY,
    ]
    blob = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def token_budget_bucket(budget: Any) -> int:
    try:
        n = int(budget or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = int(settings.code_ask_token_budget)
    for b in _BUDGET_BUCKETS:
        if n <= b:
            return b
    return _BUDGET_BUCKETS[-1]


@dataclass(frozen=True)
class CacheKey:
    """Every input besides the question, the namespace and the embedding
    model (row columns the lookup filters on) that shapes an answer."""

    scope: str
    rerank: str
    budget_bucket: int
    synth_model: str
    prompt_version: str
    memory_namespace: str

    def digest(self) -> str:
        blob = json.dumps(
            [
                self.scope,
                self.rerank,
                self.budget_bucket,
                self.synth_model,
                self.prompt_version,
                self.memory_namespace,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def cache_key_for(search_kwargs: dict[str, Any], synth_model: str) -> CacheKey:
    scope = {
        k: (sorted(v) if isinstance(v, (list, tuple, set)) and k != "pattern" else v)
        for k in _SCOPE_ARGS
        if (v := search_kwargs.get(k)) not in (None, "", [], ())
    }
    return CacheKey(
        scope=json.dumps(scope, sort_keys=True, separators=(",", ":"), default=str),
        rerank=str(search_kwargs.get("rerank_endpoint") or ""),
        budget_bucket=token_budget_bucket(search_kwargs.get("token_budget")),
        synth_model=synth_model,
        prompt_version=prompt_version(),
        memory_namespace=settings.code_ask_memory_namespace.strip(),
    )


def _synth_model_of(llm: Any) -> str:
    """The model the synthesis runs (or would run) on. An ask with no client
    (retrieval-only, over the namespace's cost cap) keys on the configured
    model: it may be served the entry that model wrote."""
    model = str(getattr(llm, "model", "") or "") if llm is not None else ""
    return model or str(settings.code_ask_llm_model or settings.llm_model or "")
