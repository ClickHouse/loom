import { getJson, post } from "../api";

export type LlmProvider = "openai" | "anthropic";
export type EmbeddingProvider = "openai" | "gemini" | "compatible" | "local";
/** The providers that take an API key: the local embedder runs on the server. */
export type ModelProvider = LlmProvider | Exclude<EmbeddingProvider, "local">;

/** The local embedder's models and their widths (server/settings/catalog.py). */
export const LOCAL_MODELS = {
  local: { label: "bge-small (default)", dimensions: 384 },
  "local:bge-base": { label: "bge-base", dimensions: 768 },
  "local:minilm": { label: "MiniLM", dimensions: 384 },
} as const;

export const MODEL_JOBS = {
  llm_fallback_model: "Fallback model (optional)",
  memory_ask_synth_model: "Memory answers",
  memory_ask_synth_model_cheap: "Low-cost answer tier (optional)",
  memory_ask_mid_model: "Middle answer tier (optional)",
  profile_consolidation_model: "Profile consolidation",
  reflection_distill_model: "Reflection",
  code_link_revise_model: "Code-linked memory checks",
  session_brief_llm_model: "Session briefs",
  code_ask_llm_model: "Code answers",
  code_search_hyde_model: "Code search expansion (optional)",
  code_search_concept_expand_model: "Concept expansion (optional)",
  code_bisect_llm_rerank_model: "Change-history ranking",
  code_summary_model: "Code summaries",
  tracker_dupcheck_rerank_model: "Duplicate ranking",
  tracker_dupcheck_judge_model: "Duplicate judging",
  retrieval_judge_model: "Retrieval judging",
} as const;

export interface ModelConfiguration {
  llm: {
    provider: LlmProvider;
    model: string;
    overrides: Partial<Record<keyof typeof MODEL_JOBS, string>>;
    verification_models: string[] | null;
  };
  embedding: {
    provider: EmbeddingProvider;
    model: string;
    dimensions: number;
    base_url?: string;
    send_dimensions?: boolean;
  };
  keys: Partial<Record<ModelProvider, string>>;
  expected_version: string;
}

export interface ModelStatus {
  version: string;
  source: "environment" | "dashboard";
  llm_provider: string;
  llm_model: string;
  embedding_provider: string;
  embedding_model: string;
  embedding_dimensions: number;
  embedding_base_url?: string;
  embedding_send_dimensions?: boolean;
  configured_keys: Partial<Record<ModelProvider, boolean>>;
  requires_setup: boolean;
  llm_overrides: ModelConfiguration["llm"]["overrides"];
  verification_models: string[] | null;
  pending?: { config_id: string; state: string; error_code: string; progress: Record<string, string | number> } | null;
  embedding_change_blocked?: string;
}

export interface ModelValidation {
  ready: boolean;
  checks: {
    role: "llm" | "embedding";
    provider: string;
    model: string;
    ready: boolean;
    code: string;
    detail?: string;
  }[];
}

export interface ModelSaveResult {
  config_id: string;
  state: string;
}

export const getModelStatus = (signal?: AbortSignal) => getJson<ModelStatus>("/v1/admin/models", { signal });
export const validateModelConfiguration = (body: ModelConfiguration) =>
  post<ModelValidation>("/v1/admin/models/validate", body);
export const saveModelConfiguration = (body: ModelConfiguration) => post<ModelSaveResult>("/v1/admin/models", body);
export const resetModelConfiguration = (expected_version: string) =>
  post<ModelSaveResult>("/v1/admin/models/reset", { expected_version });
export const retryModelConfiguration = (config_id: string) =>
  post<ModelSaveResult>("/v1/admin/models/retry", { config_id });
