"""Models: which model does each job.

Every setting that names a model lives here, one per job, so a deployment changes
a job's model with one env var (`LOOM_<FIELD>`). The provider is inferred from the
name (`claude*` → Anthropic, `gpt*` / o-series → OpenAI), so any job can run on
either provider when its API key is set. "" means the job uses `llm_model`.
"""

from __future__ import annotations

from pydantic import BaseModel


class ModelSettings(BaseModel):
    # --- defaults ---------------------------------------------------------------
    # The deployment's LLM: memory extraction, the conflict judge, rerank and every
    # job below left "". Empty takes the resolved provider's default model
    # (gpt-6-luna / claude-haiku-4-5 / fake-echo-1).
    llm_model: str = ""
    # Model to answer with when a client's provider is still unavailable after
    # its retries (429 / 5xx / transport / timeout) or rejected the request
    # (400 / 413 / 422). Counted in `loom.llm.fallback`. 401 / 403 / 404 are
    # configuration and never fall back. "" = fail the op.
    llm_fallback_model: str = ""
    # Embeddings for memories, queries and code chunks. fake-ngram-3072 needs no
    # network or key; production uses text-embedding-3-* (OPENAI_API_KEY). Change
    # it together with `embedding_dim`; a new embedding space needs a full re-embed.
    embedding_model: str = "fake-ngram-3072"

    # --- memory -----------------------------------------------------------------
    # memory.ask synthesis (the strong tier when the cascade is on).
    memory_ask_synth_model: str = ""
    # memory.ask cheap tier. "" = off: every ask uses memory_ask_synth_model. When
    # set, server/memory/domain/synth_policy.py routes easy asks cheap and hard ones
    # strong; an empty, uncited or hedging cheap answer escalates to strong once.
    # route_tier / route_reason / synth_model on the output record the choice.
    memory_ask_synth_model_cheap: str = ""
    # memory.ask mid tier. "" = none. With a model set, strong-routed asks whose
    # reason is in memory_ask_synth_mid_routes go mid; an empty, uncited or hedging
    # mid answer escalates to strong once.
    memory_ask_mid_model: str = ""
    # The durability check on a coding capture: one batched call per capture per
    # listed model (CSV) judges each fact durable or not and folds same-claim
    # paraphrases and replaced states. A fact is dropped only when every model says
    # drop; folds come from the first. Fails open. "" turns it off.
    memory_extract_verify_models: str = "claude-opus-5-5"
    # The profile fold.
    profile_consolidation_model: str = ""
    # Reflection distillation: the outcome labeller and the contrast writer.
    reflection_distill_model: str = ""
    # Session briefs; without its provider's key the deployment's LLM writes them.
    session_brief_llm_model: str = "gpt-6-luna"

    # --- code -------------------------------------------------------------------
    # code.ask synthesis. Answers are memoized fleet-wide, so a stronger model costs
    # once per unique question.
    code_ask_llm_model: str = ""
    # code.search HyDE: writes a hypothetical indexed passage whose embedding runs a
    # second vector list (see code_search_hyde_*). "" = off.
    code_search_hyde_model: str = ""
    # code.search concept expansion: maps a prose, unconstrained question to likely
    # code identifiers — via this model, or `cards` / `cards:<n>` (symbols of the n
    # nearest file cards) — fused as a capped lexical list. "" = off.
    code_search_concept_expand_model: str = ""
    # code.bisect_candidates' one-call rerank of the fused top 20.
    code_bisect_llm_rerank_model: str = "gpt-6-luna"
    # File and module summary cards.
    code_summary_model: str = ""

    # --- tracker ----------------------------------------------------------------
    # tracker.duplicate_check's rerank call.
    tracker_dupcheck_rerank_model: str = "gpt-6-luna"
    # tracker.duplicate_check's judge call.
    tracker_dupcheck_judge_model: str = ""

    # --- observability ----------------------------------------------------------
    # The sampled retrieval judge (retrieval_judge_sample_rate). Deliberately a
    # different model from the ones answering: a same-model judge measures
    # agreement, not quality.
    retrieval_judge_model: str = "gpt-6.1-sol"
