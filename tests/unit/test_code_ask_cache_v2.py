"""The code.ask answer cache v2.

A hit must match the complete cache key, pass the identifier guard and find
every cited and shown file unchanged and the entry young enough; an abstain
is cached for its exact text under the index head; a release branch reuses
its trunk's entry inside one access boundary; a negative outcome tombstones
the served entry; the pre-synthesis LLM calls read the exact-prompt cache;
an unresolved namespace is never read or written."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app import answers as ca
from server.code.app import answers_cache, answers_entry
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.settings import settings

ORG = "clickhouse"
NS = "code-clickhouse"
BRANCH = "code-clickhouse-26-8"
MODEL = "text-embedding-3-large"
HEAD = "a" * 40


# ---- fakes -------------------------------------------------------------------


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class _CH:
    """code_answers as a ReplacingMergeTree (newest version per key wins)
    plus code_files hashes; every statement is recorded."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.files: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def live(self) -> list[dict[str, Any]]:
        latest: dict[tuple[Any, ...], dict[str, Any]] = {}
        for r in self.rows:
            k = (r["org"], r["namespace"], r["embedding_model"], str(r["question_id"]))
            if k not in latest or r["version"] >= latest[k]["version"]:
                latest[k] = r
        return list(latest.values())

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        p = dict(params or {})
        self.calls.append((sql, p))
        if "FROM loom.code_files" in sql:
            return [
                {"path": path, "content_hash": h}
                for (ns, path), h in self.files.items()
                if ns == p["ns"] and path in p["paths"]
            ]
        if "cosineDistance" in sql:
            out = []
            for r in self.live():
                if (r["org"], r["namespace"], r["embedding_model"]) != (
                    p["org"],
                    p["ns"],
                    p["model"],
                ):
                    continue
                if r["tombstoned_at"] is not None or not r["answer"] or r["entry_kind"] != "answer":
                    continue
                out.append(
                    {
                        **r,
                        "age_s": r.get("_age_s", 0),
                        "distance": 1.0 - _cos(r["question_embedding"], p["q"]),
                    }
                )
            out.sort(key=lambda r: r["distance"])
            return out[: p["k"]]
        if "SELECT * EXCEPT" in sql:
            return [
                r
                for r in self.live()
                if r["namespace"] == p["ns"]
                and str(r["question_id"]) == p["qid"]
                and r["tombstoned_at"] is None
            ]
        raise AssertionError(f"unexpected statement: {sql}")

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        p = dict(params or {})
        self.calls.append((sql, p))
        assert "entry_kind = 'negative'" in sql
        for r in self.live():
            if (
                (r["org"], r["namespace"], r["embedding_model"], str(r["question_id"]))
                == (p["org"], p["ns"], p["model"], p["qid"])
                and r["tombstoned_at"] is None
                and r["entry_kind"] == "negative"
            ):
                return {**r, "age_s": r.get("_age_s", 0)}
        return None


class _Repo(CodeRepo):
    """The real store's answer-cache statements over `_CH`; inserts recorded."""

    def __init__(self, ch: _CH | None = None) -> None:
        super().__init__(ch or _CH())  # type: ignore[arg-type]
        self.inserts: list[dict[str, Any]] = []

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert table == "code_answers"
        self.inserts.extend(rows)
        self._ch.rows.extend(dict(r) for r in rows)

    async def functions_for_paths(self, *a: Any, **k: Any) -> list[Any]:
        return []


class _Embedder:
    model_name = MODEL

    def __init__(self, vecs: dict[str, list[float]] | None = None) -> None:
        self.vecs = vecs or {}

    async def embed(self, text: str) -> list[float]:
        return self.vecs.get(text, [1.0, 0.0, 0.0])


class _LLM:
    provider = "fake"
    model = "gpt-4o"

    def __init__(
        self, answer: str = "TTL drops parts in [src/a.cpp:1-10].\nCOVERAGE: full"
    ) -> None:
        self.answer = answer
        self.expand_calls = 0
        self.synth_calls = 0

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        if system == answers_entry._EXPAND_SYSTEM:
            self.expand_calls += 1
            return SimpleNamespace(
                text="MergeTreeDataMergerMutator TTLDelete",
                model=self.model,
                tokens_in=10,
                tokens_out=5,
            )
        self.synth_calls += 1
        return SimpleNamespace(text=self.answer, model=self.model, tokens_in=100, tokens_out=50)


def _units() -> list[CodeUnit]:
    return [
        CodeUnit(unit_id="u1", path="src/a.cpp", start_line=1, end_line=10, symbol="A", body="a"),
        CodeUnit(unit_id="u2", path="src/b.cpp", start_line=1, end_line=10, symbol="B", body="b"),
    ]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A deterministic ask: retrieval returns two units (a.cpp cited, b.cpp
    shown), no memory context, no LLM response cache."""
    import server.code.app.search as cs

    state: dict[str, Any] = {"searches": 0, "abstain": False}

    async def _search(*a: Any, **k: Any) -> Any:
        state["searches"] += 1
        return SimpleNamespace(
            abstain=state["abstain"],
            abstain_reason="no strong match in the code index",
            units=[] if state["abstain"] else _units(),
            history={},
            retrieval_confidence=0.5,
        )

    async def _mem(q: str) -> list[str]:
        return []

    monkeypatch.setattr(cs, "search", _search)
    monkeypatch.setattr(ca, "_inject_memory_context", _mem)
    monkeypatch.setattr(settings, "code_ask_memory_namespace", "")
    monkeypatch.setattr(settings, "code_ask_cache_threshold", 0.0)
    monkeypatch.setattr(settings, "code_ask_cache_max_age_days", 14)
    monkeypatch.setattr(settings, "code_ask_negative_cache_ttl_s", 3600)
    monkeypatch.setattr(settings, "code_ask_llm_cache", False)
    monkeypatch.setattr(settings, "code_ask_token_budget", 4000)
    repo = _Repo()
    repo._ch.files.update({(NS, "src/a.cpp"): "ha", (NS, "src/b.cpp"): "hb"})
    state["repo"] = repo
    return state


def _ask(
    repo: _Repo,
    question: str,
    *,
    ns: str = NS,
    llm: Any = None,
    embedder: Any = None,
    kwargs: dict[str, Any] | None = None,
    no_cache: bool = False,
    head: str = HEAD,
) -> answers_entry.AskResult:
    return asyncio.run(
        ca.ask(
            repo,
            ORG,
            ns,
            question=question,
            embedder=embedder or _Embedder(),  # type: ignore[arg-type]
            llm=llm if llm is not None else _LLM(),
            search_kwargs=dict(kwargs or {"top_k": 10, "token_budget": 4000}),
            no_cache=no_cache,
            index_head=head,
        )
    )


# ---- 1. the complete cache key -------------------------------------------------


def test_the_key_covers_scope_rerank_budget_model_and_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = answers_entry.cache_key_for({"top_k": 10, "token_budget": 4000}, "gpt-4o")
    same_bucket = answers_entry.cache_key_for({"top_k": 10, "token_budget": 3000}, "gpt-4o")
    assert base.digest() == same_bucket.digest()
    for other in (
        answers_entry.cache_key_for({"top_k": 10, "token_budget": 8000}, "gpt-4o"),
        answers_entry.cache_key_for(
            {"top_k": 10, "token_budget": 4000, "rerank_endpoint": "http://r"}, "gpt-4o"
        ),
        answers_entry.cache_key_for(
            {"top_k": 10, "token_budget": 4000, "path_prefix": "src/Storages/"}, "gpt-4o"
        ),
        answers_entry.cache_key_for(
            {"top_k": 10, "token_budget": 4000, "langs": ["python"]}, "gpt-4o"
        ),
        answers_entry.cache_key_for(
            {"top_k": 10, "token_budget": 4000, "pattern": "TTL"}, "gpt-4o"
        ),
        answers_entry.cache_key_for({"top_k": 10, "token_budget": 4000}, "gpt-4o-mini"),
    ):
        assert other.digest() != base.digest()
    assert answers_entry.token_budget_bucket(None) == 4000  # the deployment default
    assert answers_entry.token_budget_bucket(12000) == 12000
    v1 = answers_entry.prompt_version()
    monkeypatch.setattr(answers_entry, "_SYNTH_SYSTEM", answers_entry._SYNTH_SYSTEM + " Be brief.")
    monkeypatch.setattr(ca, "_SYNTH_SYSTEM", answers_entry._SYNTH_SYSTEM + " Be brief.")
    assert answers_entry.prompt_version() != v1
    assert (
        answers_entry.cache_key_for({}, "gpt-4o").digest()
        != answers_entry.cache_key_for({}, "gpt-4o")
        .__class__(**{**answers_entry.cache_key_for({}, "gpt-4o").__dict__, "prompt_version": v1})
        .digest()
    )


def test_a_hit_needs_the_same_key_and_a_mismatch_is_a_miss(env: dict[str, Any]) -> None:
    repo = env["repo"]
    q = "how does TTL drop old parts?"
    first = _ask(repo, q)
    assert first.source == "synthesized" and first.cache == "miss"
    row = repo.inserts[-1]
    assert row["cache_key"] and row["prompt_version"] == answers_entry.prompt_version()
    assert row["llm_model"] == "gpt-4o" and row["entry_kind"] == "answer"
    hit = _ask(repo, q)
    assert hit.source == "cached" and hit.cache == "hit" and hit.cache_similarity == 1.0
    other = _ask(repo, q, kwargs={"top_k": 10, "token_budget": 8000})
    assert other.source == "synthesized" and other.cache == "key_mismatch"
    # both entries live side by side: the key rides inside the RMT key
    assert len({(r["question_id"]) for r in repo._ch.live() if r["tombstoned_at"] is None}) == 2


def test_a_hit_does_not_pay_for_the_identifier_proposal(env: dict[str, Any]) -> None:
    repo, q = env["repo"], "how does TTL drop old parts?"
    _ask(repo, q)
    llm = _LLM()
    assert _ask(repo, q, llm=llm).cache == "hit"
    assert llm.expand_calls == 0 and llm.synth_calls == 0 and env["searches"] == 1


def test_a_narrowed_memory_reader_neither_reads_nor_writes_the_cache(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.domain.read_scope import (
        WHOLE_NAMESPACE,
        bind_request_read_filters,
        resolve_read_filters,
    )

    canary = "CANARY-u2-memory"

    async def _mem(q: str) -> list[str]:
        return [f"[u2/decision] {canary}"]

    async def _public() -> bool:
        return True  # a public memory namespace does not lift the token's ceiling

    monkeypatch.setattr(settings, "code_ask_memory_namespace", "decisions")
    monkeypatch.setattr(ca, "_inject_memory_context", _mem)
    monkeypatch.setattr(ca, "_memory_context_cacheable", _public)
    repo, q = env["repo"], "how does TTL drop old parts?"
    # A namespace-wide reader warms the cache with an answer citing u2's memory.
    warm = _ask(repo, q, llm=_LLM(f"{canary} in [src/a.cpp:1-10].\nCOVERAGE: full"))
    assert canary in warm.answer and len(repo.inserts) == 1
    pinned = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=(NS,),
        roles=(),
        is_service_account=True,
        read_user_id="u1",
    )
    try:
        set_current_auth(pinned)
        bind_request_read_filters(
            pinned, resolve_read_filters(requested=None, user_id=None, session_id=None, auth=pinned)
        )
        out = _ask(repo, q)
    finally:
        set_current_auth(None)
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    assert out.source == "synthesized" and out.cache == "bypassed"
    assert canary not in out.answer
    assert len(repo.inserts) == 1  # nothing written for the narrowed caller either


# ---- 2. the identifier guard ---------------------------------------------------


def test_load_bearing_tokens() -> None:
    t = answers_cache.load_bearing_tokens
    assert t("where is max_threads applied?") == {"max_threads"}
    assert t("where does toStartOfInterval throw ILLEGAL_TYPE_OF_ARGUMENT?") == {
        "tostartofinterval",
        "illegal_type_of_argument",
    }
    assert t("how does StorageReplicatedMergeTree::fetchPart work") == {
        "storagereplicatedmergetree::fetchpart"
    }
    assert t("what does src/Interpreters/InterpreterSelectQuery.cpp do") == {
        "src/interpreters/interpreterselectquery.cpp"
    }
    assert t("was PR #119663 merged in 26.8?") == {"119663", "26.8"}
    assert t("how does a qa.* call reach tracker.duplicate_check") == {
        "qa.*",
        "tracker.duplicate_check",
    }
    assert t("where is the 'max_parts_in_total' limit checked") == {"max_parts_in_total"}
    assert t("how does ReadBufferFromS3 retry S3 requests") == {"readbufferfroms3", "s3"}
    assert t("how does ClickHouse drop old parts?") == frozenset()  # the product's own name
    assert t("how are parts renamed after an insert?") == frozenset()


def test_max_threads_is_never_served_for_max_insert_threads(env: dict[str, Any]) -> None:
    """Two questions naming different settings can embed very close (cosine
    0.8797 measured); even at cosine 1.0 the guard refuses a question naming
    another setting."""
    repo = env["repo"]
    same = [0.3, 0.4, 0.5]
    emb = _Embedder(
        {"where is max_threads applied?": same, "where is max_insert_threads applied?": same}
    )
    _ask(repo, "where is max_threads applied?", embedder=emb)
    out = _ask(repo, "where is max_insert_threads applied?", embedder=emb)
    assert out.source == "synthesized" and out.cache == "guarded"
    assert not answers_cache.guard_passes(
        "where is max_threads applied?", "where is max_insert_threads applied?"
    )
    # a paraphrase naming the same setting still hits
    emb.vecs["where does the max_threads setting take effect?"] = same
    assert (
        _ask(repo, "where does the max_threads setting take effect?", embedder=emb).cache == "hit"
    )


def test_thresholds_stay_per_model() -> None:
    assert answers_entry.CACHE_HIT_THRESHOLDS == {
        "text-embedding-3-small": 0.87,
        "text-embedding-3-large": 0.85,
    }
    assert answers_entry.cache_hit_threshold("unlisted-model") == max(
        answers_entry.CACHE_HIT_THRESHOLDS.values()
    )


def test_the_measured_summary_backs_the_table() -> None:
    from pathlib import Path

    here = Path(__file__).resolve().parents[2] / "bench" / "code_ask_cache_threshold"
    large = json.loads((here / "summary.json").read_text())
    small = json.loads((here / "summary_3small.json").read_text())
    # The shipped value is at or above the lowest measured zero-false-hit
    # threshold, and the sweep row at the shipped value has no false hit
    # (3-large ships 0.85 over the measured 0.84 for margin).
    for summary, model in ((large, "text-embedding-3-large"), (small, "text-embedding-3-small")):
        shipped = answers_entry.CACHE_HIT_THRESHOLDS[model]
        assert summary["chosen_with_guard"]["false_hits"] == 0
        assert shipped >= summary["chosen_with_guard"]["threshold"]
        at = [r for r in summary["sweep_with_guard"] if abs(r["threshold"] - shipped) < 1e-9]
        # the sweep is a 0.05 grid; a shipped value off it is backed by the chosen row itself
        assert at or shipped == summary["chosen_with_guard"]["threshold"], model
        assert all(r["false_hits"] == 0 for r in at), model  # false hits only fall as it rises
    raw = (here / "pairs.json").read_bytes()
    pairs = json.loads(raw)
    for summary in (large, small):
        # measured on the pairs as they are now: same counts, and the same bytes
        # once measure.py has stamped them
        assert summary["paraphrase"]["n"] == len(pairs["paraphrase"])
        assert summary["different"]["n"] == len(pairs["different"])
        assert summary["different_identifier"]["n"] == len(pairs["different_identifier"])
        if "pairs_sha256" in summary:
            assert summary["pairs_sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(pairs["different_identifier"]) >= 20
    assert not any(answers_cache.guard_passes(a, b) for a, b in pairs["different_identifier"])


# ---- 3. invalidation -----------------------------------------------------------


def test_a_changed_cited_file_is_stale_and_tombstoned(env: dict[str, Any]) -> None:
    repo, q = env["repo"], "how does TTL drop old parts?"
    _ask(repo, q)
    repo._ch.files[(NS, "src/a.cpp")] = "ha2"
    out = _ask(repo, q)
    assert out.source == "synthesized" and out.cache == "stale"
    assert any(r["tombstoned_at"] is not None and r["namespace"] == NS for r in repo.inserts)


def test_a_changed_shown_but_uncited_file_is_stale(env: dict[str, Any]) -> None:
    repo, q = env["repo"], "how does TTL drop old parts?"
    _ask(repo, q)
    row = repo.inserts[-1]
    assert row["cited_paths"] == ["src/a.cpp"] and row["retrieval_paths"] == ["src/b.cpp"]
    assert row["retrieval_hashes"] == ["hb"]
    before = len([c for c in repo._ch.calls if "loom.code_files" in c[0]])
    assert _ask(repo, q).cache == "hit"
    # one keyed IN read covers the cited and the shown files
    reads = [c for c in repo._ch.calls if "loom.code_files" in c[0]][before:]
    assert len(reads) == 1 and sorted(reads[0][1]["paths"]) == ["src/a.cpp", "src/b.cpp"]
    repo._ch.files[(NS, "src/b.cpp")] = "hb2"
    assert _ask(repo, q).cache == "stale"


def test_an_entry_past_the_max_age_is_stale(env: dict[str, Any]) -> None:
    repo, q = env["repo"], "how does TTL drop old parts?"
    _ask(repo, q)
    repo._ch.rows[-1]["_age_s"] = 15 * 86400
    out = _ask(repo, q)
    assert out.cache == "stale" and out.source == "synthesized"
    assert repo.inserts[-2]["tombstoned_at"] is not None  # tombstoned lazily, no TTL clause


def test_a_prompt_change_invalidates_without_a_purge(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, q = env["repo"], "how does TTL drop old parts?"
    _ask(repo, q)
    monkeypatch.setattr(answers_entry, "_ANSWER_POLICY", answers_entry._ANSWER_POLICY + " (rev)")
    assert _ask(repo, q).cache == "stale"


def test_a_row_without_cache_fields_is_stale_not_served(env: dict[str, Any]) -> None:
    from server.code.store.index import build_row

    repo, q = env["repo"], "how does TTL drop old parts?"
    repo._ch.rows.append(
        build_row(
            "code_answers",
            org=ORG,
            namespace=NS,
            question_id=answers_entry.question_id(q),
            question=q,
            question_embedding=[1.0, 0.0, 0.0],
            embedding_model=MODEL,
            answer="old",
            citations="[]",
        )
    )
    out = _ask(repo, q)
    assert out.cache == "stale" and out.answer != "old"


# ---- 4. the negative cache ------------------------------------------------------


def test_an_abstain_is_cached_for_its_exact_text_under_the_index_head(env: dict[str, Any]) -> None:
    repo = env["repo"]
    env["abstain"] = True
    q = "how does the server encrypt data at rest?"
    first = _ask(repo, q)
    assert first.abstain and first.cache == "miss" and env["searches"] == 1
    assert repo.inserts[-1]["entry_kind"] == "negative"
    assert repo.inserts[-1]["indexed_commit"] == HEAD
    again = _ask(repo, "How does the server encrypt data at rest")  # case / punctuation folded
    assert again.abstain and again.cache == "negative" and env["searches"] == 1
    # never semantic: a paraphrase at cosine 1.0 retrieves again
    para = _ask(repo, "where is data at rest encrypted?")
    assert para.cache != "negative" and env["searches"] == 2
    # a new merge re-opens it
    assert _ask(repo, q, head="b" * 40).cache != "negative" and env["searches"] == 3


def test_a_negative_entry_expires(env: dict[str, Any]) -> None:
    repo = env["repo"]
    env["abstain"] = True
    q = "how does the server encrypt data at rest?"
    _ask(repo, q)
    repo._ch.rows[-1]["_age_s"] = 3601
    assert _ask(repo, q).cache != "negative"


def test_no_negative_entry_without_an_index_head(env: dict[str, Any]) -> None:
    repo = env["repo"]
    env["abstain"] = True
    _ask(repo, "how does the server encrypt data at rest?", head="")
    assert not repo.inserts


# ---- 5. cross-branch reuse -----------------------------------------------------


@pytest.fixture
def branch(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import server.auth.authz as authz
    import server.code.domain.index.repos as repos
    import server.identity.app.namespace_classification as nc
    import server.identity.app.namespace_visibility as nv

    cfg = SimpleNamespace(embed_reuse_from=NS)
    monkeypatch.setattr(repos, "repo_for_namespace", lambda org, ns: cfg if ns == BRANCH else None)
    env["restricted"] = set()
    env["vis"] = {NS: "private", BRANCH: "private"}
    env["readable"] = True

    async def _restricted(org: str, ns: str) -> bool:
        return ns in env["restricted"]

    async def _vis(org: str, namespaces: list[str]) -> dict[str, str]:
        return {n: env["vis"][n] for n in namespaces if n in env["vis"]}

    async def _scope(org: str, ns: str, role: Any = None, *, additive: bool = False) -> bool:
        return bool(env["readable"])

    monkeypatch.setattr(nc, "is_restricted", _restricted)
    monkeypatch.setattr(nv, "visibility_map", _vis)
    monkeypatch.setattr(authz, "row_in_scope", _scope)
    repo = env["repo"]
    repo._ch.files.update({(BRANCH, "src/a.cpp"): "ha", (BRANCH, "src/b.cpp"): "hb"})
    _ask(repo, "how does TTL drop old parts?")  # the trunk entry
    return env


def test_a_branch_miss_reuses_the_trunk_entry_when_its_files_agree(branch: dict[str, Any]) -> None:
    repo = branch["repo"]
    llm = _LLM()
    out = _ask(repo, "how does TTL drop old parts?", ns=BRANCH, llm=llm)
    assert out.source == "cached" and out.cache == f"reused_from:{NS}"
    assert llm.synth_calls == 0
    copy = repo.inserts[-1]
    assert copy["namespace"] == BRANCH and copy["reused_from"] == NS
    assert copy["cited_hashes"] == ["ha"] and copy["retrieval_hashes"] == ["hb"]
    assert _ask(repo, "how does TTL drop old parts?", ns=BRANCH).cache == "hit"


def test_a_branch_whose_files_differ_does_not_reuse(branch: dict[str, Any]) -> None:
    repo = branch["repo"]
    repo._ch.files[(BRANCH, "src/b.cpp")] = "hb-26.8"
    out = _ask(repo, "how does TTL drop old parts?", ns=BRANCH)
    assert out.source == "synthesized" and not out.cache.startswith("reused_from")
    # the trunk entry was only borrowed: never tombstoned for the branch's sake
    assert _ask(repo, "how does TTL drop old parts?").cache == "hit"


@pytest.mark.parametrize("restricted", [{NS}, {BRANCH}])
def test_reuse_never_crosses_a_restricted_boundary(
    branch: dict[str, Any], restricted: set[str]
) -> None:
    branch["restricted"] = restricted
    out = _ask(branch["repo"], "how does TTL drop old parts?", ns=BRANCH)
    assert out.source == "synthesized" and not out.cache.startswith("reused_from")


def test_reuse_needs_the_same_visibility_and_a_readable_trunk(branch: dict[str, Any]) -> None:
    branch["vis"][NS] = "public"
    assert not _ask(branch["repo"], "how does TTL drop old parts?", ns=BRANCH).cache.startswith(
        "reused"
    )
    branch["vis"][NS] = "private"
    branch["readable"] = False
    assert not _ask(branch["repo"], "how are TTL parts dropped?", ns=BRANCH).cache.startswith(
        "reused"
    )


# ---- 6. feedback invalidation ----------------------------------------------------


def test_a_negative_outcome_on_a_cached_answer_tombstones_it(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.api.models_outcome import CodeOutcomeInput
    from server.code.app import outcome

    repo, q = env["repo"], "how does TTL drop old parts?"
    _ask(repo, q)
    served = _ask(repo, q)
    assert served.cache == "hit" and served.cache_entry["namespace"] == NS
    trace = {
        "op": "code.ask",
        "route": "cached",
        "returned_ids": ["u1"],
        "returned_paths": ["src/a.cpp"],
        "abstain": 0,
        "args_json": json.dumps({"cache": "hit", "cache_entry": served.cache_entry}),
    }

    class _TraceRepo(_Repo):
        async def trace_by_run_id(self, org: str, ns: str, rid: str) -> dict[str, Any]:
            return trace

        async def add_unit_signals(self, *a: Any) -> None:
            return None

    trepo = _TraceRepo(repo._ch)
    monkeypatch.setattr(outcome, "CodeRepo", lambda: trepo)
    asyncio.run(
        outcome._fold_outcome_signal(
            ORG,
            NS,
            CodeOutcomeInput(org=ORG, namespace=NS, trace_run_id="t", action="used_in_answer"),
        )
    )
    assert not trepo.inserts  # a positive outcome keeps the entry
    asyncio.run(
        outcome._fold_outcome_signal(
            ORG,
            NS,
            CodeOutcomeInput(org=ORG, namespace=NS, trace_run_id="t", action="fell_back_grep"),
        )
    )
    assert trepo.inserts and trepo.inserts[-1]["tombstoned_at"] is not None
    assert _ask(repo, q).cache != "hit"


def test_only_a_cached_ask_trace_names_an_entry(env: dict[str, Any]) -> None:
    from server.code.app import outcome

    repo = env["repo"]
    entry = json.dumps({"cache_entry": {"namespace": NS, "question_id": "not-a-uuid"}})
    for trace in (
        {"op": "code.ask", "route": "synthesized", "args_json": entry},
        {"op": "code.search", "route": "cached", "args_json": entry},
        {"op": "code.ask", "route": "abstained", "args_json": entry},  # a negative-cache abstain
        {"op": "code.ask", "route": "cached", "args_json": "{not json"},
        {"op": "code.ask", "route": "cached", "args_json": entry},  # a malformed id reads nothing
    ):
        assert asyncio.run(outcome._invalidate_cached_answer(repo, ORG, trace)) == 0  # type: ignore[arg-type]
    assert (
        "fell_back_grep" in outcome._NEGATIVE_OUTCOMES and "ignored" in outcome._NEGATIVE_OUTCOMES
    )
    assert "used_in_answer" not in outcome._NEGATIVE_OUTCOMES


def test_a_reused_copy_tombstones_its_trunk_entry_too(branch: dict[str, Any]) -> None:
    repo = branch["repo"]
    out = _ask(repo, "how does TTL drop old parts?", ns=BRANCH)
    assert out.cache_entry["trunk_namespace"] == NS
    n = asyncio.run(answers_cache.tombstone_served_entry(repo, ORG, out.cache_entry))  # type: ignore[arg-type]
    assert n == 2


# ---- 7. the exact LLM cache for the pre-synthesis calls -----------------------------


class _LLMCacheCH:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        return self.rows.get((params or {})["key"])

    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        for r in rows:
            self.rows[r["key"]] = r


@pytest.fixture
def llm_cache(monkeypatch: pytest.MonkeyPatch) -> _LLMCacheCH:
    from server.llm import cache as lc

    ch = _LLMCacheCH()
    monkeypatch.setattr(lc.CachedLLMClient, "_client", lambda self: ch)
    monkeypatch.setattr(settings, "code_ask_llm_cache", True)
    return ch


async def _flush() -> None:
    from server.llm import cache as lc

    for _ in range(3):
        await asyncio.sleep(0)
    if lc._TASKS:
        await asyncio.gather(*list(lc._TASKS))


def test_the_identifier_proposal_is_answered_once_per_namespace(
    env: dict[str, Any], llm_cache: _LLMCacheCH
) -> None:
    repo = env["repo"]
    llm = _LLM(answer="nothing here\nCOVERAGE: none")  # never memoized: every ask retrieves

    async def run() -> None:
        for _ in range(2):
            await ca.ask(
                repo,
                ORG,
                NS,
                question="how does TTL drop old parts?",  # type: ignore[arg-type]
                embedder=_Embedder(),
                llm=llm,
                search_kwargs={"top_k": 10},
                index_head=HEAD,
            )
            await _flush()
        assert llm.expand_calls == 1 and llm.synth_calls == 2
        # another namespace keys apart
        await ca.ask(
            repo,
            ORG,
            "code-loom",
            question="how does TTL drop old parts?",  # type: ignore[arg-type]
            embedder=_Embedder(),
            llm=llm,
            search_kwargs={"top_k": 10},
            index_head=HEAD,
        )
        assert llm.expand_calls == 2
        # no_cache (the bench) calls the provider
        await ca.ask(
            repo,
            ORG,
            NS,
            question="how does TTL drop old parts?",  # type: ignore[arg-type]
            embedder=_Embedder(),
            llm=llm,
            search_kwargs={"top_k": 10},
            no_cache=True,
        )
        assert llm.expand_calls == 3

    asyncio.run(run())


def test_the_retrieval_legs_read_the_cache_only_inside_the_ask_scope(
    llm_cache: _LLMCacheCH,
) -> None:
    from server.llm.cache import CachedLLMClient, exact_cache_scope, wrap_scoped

    inner = _LLM()
    assert wrap_scoped(inner) is inner  # plain code.search
    with exact_cache_scope(ORG, NS):
        wrapped = wrap_scoped(inner)
        assert isinstance(wrapped, CachedLLMClient) and wrapped._key_scope == f"{ORG}/{NS}"
        assert wrap_scoped(wrapped) is wrapped
    assert wrap_scoped(object()) is not None  # a client the wrapper cannot take is used as is


def test_a_sampled_call_is_never_cached(llm_cache: _LLMCacheCH) -> None:
    from server.llm.cache import CachedLLMClient

    inner = _LLM()
    c = CachedLLMClient(inner, org=ORG, namespace=NS, key_scope="s", deterministic_only=True)

    async def run() -> None:
        for _ in range(2):
            await c.complete("sys", "u", max_tokens=5, temperature=0.7)
            await _flush()
        assert inner.synth_calls == 2 and not llm_cache.rows
        for _ in range(2):
            await c.complete("sys", "u", max_tokens=5)
            await _flush()
        assert inner.synth_calls == 3

    asyncio.run(run())


def test_the_key_scope_changes_the_key_and_memory_ask_keys_are_unchanged() -> None:
    from server.llm.cache import cache_key

    assert cache_key("s", "u", 5, None, {"_scope": "o/a"}) != cache_key(
        "s", "u", 5, None, {"_scope": "o/b"}
    )
    assert cache_key("s", "u", 5, None, {}) == cache_key("s", "u", 5)


def test_the_traffic_class_policy_can_turn_the_llm_cache_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.identity.domain.traffic_class_policy as tcp

    monkeypatch.setattr(settings, "code_ask_llm_cache", True)
    monkeypatch.setattr(tcp, "llm_policy_for", lambda cls: SimpleNamespace(llm_cache=False))
    assert answers_cache._pre_synthesis_cache_on() is False
    monkeypatch.setattr(tcp, "llm_policy_for", lambda cls: SimpleNamespace(llm_cache=None))
    assert answers_cache._pre_synthesis_cache_on() is True
    monkeypatch.setattr(settings, "code_ask_llm_cache", False)
    assert answers_cache._pre_synthesis_cache_on() is False


# ---- 8. telemetry -----------------------------------------------------------------


def test_a_hit_reports_the_saved_cost_and_latency(env: dict[str, Any]) -> None:
    repo, q = env["repo"], "how does TTL drop old parts?"
    _ask(repo, q)
    repo._ch.rows[-1].update(cost_usd=0.012, latency_ms=6400)
    out = _ask(repo, q)
    assert out.cache == "hit" and out.cache_saved_ms == 6400 and out.cache_saved_usd == 0.012


def test_output_and_labels() -> None:
    from server.code.api.models_search import CodeAskOutput
    from server.code.app.common import cache_outcome_label, record_code_ask_cache

    out = CodeAskOutput(
        source="cached",
        cache=f"reused_from:{NS}",
        cache_similarity=0.9,
        cache_saved_ms=10,
        cache_saved_usd=0.01,
    )
    assert out.model_dump()["cache"] == f"reused_from:{NS}"
    assert CodeAskOutput(source="synthesized").cache == "miss"
    assert cache_outcome_label(f"reused_from:{NS}") == "reused"
    for label in ("hit", "miss", "stale", "guarded", "key_mismatch", "negative", "bypassed"):
        assert cache_outcome_label(label) == label
        record_code_ask_cache(
            cache=label, namespace=NS, saved_ms=5, saved_usd=0.001
        )  # never raises


# ---- 9. access control and the unresolved namespace ------------------------------


def test_the_lookup_reads_only_the_asked_namespace(env: dict[str, Any]) -> None:
    repo = env["repo"]
    _ask(repo, "how does TTL drop old parts?", ns="code-loom")
    _ask(repo, "how does TTL drop old parts?")
    reads = [p for sql, p in repo._ch.calls if "loom.code_answers" in sql]
    assert reads and all(p["ns"] in (NS, "code-loom") for p in reads)
    # the entry written under code-loom is not served to a code-clickhouse ask
    assert _ask(repo, "how does TTL drop old parts?", ns="code-loom").cache == "hit"
    assert [r["namespace"] for r in repo.inserts] == ["code-loom", NS]


@pytest.mark.parametrize("ns", ["", "-", "  "])
def test_an_unresolved_namespace_is_never_read_or_written(env: dict[str, Any], ns: str) -> None:
    """A stale-entry tombstone that mirrors a projection without org/namespace
    must not land a row at namespace=''."""
    repo = env["repo"]
    out = _ask(repo, "how does TTL drop old parts?", ns=ns)
    assert out.cache == "bypassed" and out.source == "synthesized"
    assert not [c for c in repo._ch.calls if "code_answers" in c[0]]
    assert not repo.inserts
    env["abstain"] = True
    assert _ask(repo, "how does TTL drop old parts?", ns=ns).cache == "bypassed"
    assert not repo.inserts
    assert asyncio.run(answers_cache._cache_lookup(repo, ORG, ns, MODEL, [1.0])) == []  # type: ignore[arg-type]


def test_a_tombstone_carries_the_entrys_own_namespace(env: dict[str, Any]) -> None:
    repo, q = env["repo"], "how does TTL drop old parts?"
    _ask(repo, q)
    repo._ch.files[(NS, "src/a.cpp")] = "changed"
    _ask(repo, q)
    tomb = [r for r in repo.inserts if r["tombstoned_at"] is not None]
    assert tomb and all(r["org"] == ORG and r["namespace"] == NS for r in tomb)
    asyncio.run(
        answers_cache._tombstone_entry(repo, {"org": "", "namespace": "", "question_id": "x"})
    )  # type: ignore[arg-type]
    assert all(r["namespace"] for r in repo.inserts)
