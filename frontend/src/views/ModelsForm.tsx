import { useState } from "react";
import {
  MODEL_JOBS,
  type EmbeddingProvider,
  type LlmProvider,
  type ModelConfiguration,
  type ModelProvider,
  type ModelStatus,
  type ModelValidation,
} from "../api/models";

const PROVIDER_LABELS = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  gemini: "Google Gemini",
  compatible: "Custom embedding provider",
};
const inputClass = "w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const errorMessages: Record<string, string> = {
  missing_key: "Add an API key for this provider.",
  invalid_credentials: "The provider rejected the key. Check its permissions and try again.",
  quota_or_rate_limit: "Check provider billing or quota, then try again.",
  model_unavailable: "This model is not available to your API account.",
  timeout: "The provider did not respond in time. Try again.",
  invalid_output: "The model response did not match the expected format or dimensions.",
  unsupported_dimensions: "The vector tables need a dimension count that is a multiple of 8.",
  unsupported_provider: "This provider is not supported for this job.",
  provider_error: "The provider check failed. Check the model and provider availability.",
};

export function initialModelConfiguration(status: ModelStatus): ModelConfiguration {
  const llm = status.llm_provider === "anthropic" ? "anthropic" : "openai";
  const embedding =
    status.embedding_provider === "compatible"
      ? "compatible"
      : status.embedding_provider === "gemini"
        ? "gemini"
        : "openai";
  return {
    llm: {
      provider: llm,
      model:
        ["openai", "anthropic"].includes(status.llm_provider) && status.llm_model
          ? status.llm_model
          : llm === "anthropic"
            ? "claude-haiku-4-5"
            : "gpt-6-luna",
      overrides: status.llm_overrides ?? {},
      verification_models: status.verification_models ?? null,
    },
    embedding: {
      provider: embedding,
      model:
        ["openai", "gemini", "compatible"].includes(status.embedding_provider) && status.embedding_model
          ? status.embedding_model
          : embedding === "gemini"
            ? "gemini-embedding-001"
            : "text-embedding-3-large",
      dimensions: ["openai", "gemini", "compatible"].includes(status.embedding_provider)
        ? status.embedding_dimensions
        : 3072,
      ...(embedding === "compatible"
        ? { base_url: status.embedding_base_url ?? "", send_dimensions: status.embedding_send_dimensions ?? false }
        : {}),
    },
    keys: {},
    expected_version: status.version,
  };
}

interface Props {
  status: ModelStatus;
  onValidate: (configuration: ModelConfiguration) => Promise<ModelValidation>;
  onSave: (configuration: ModelConfiguration) => Promise<void>;
}

export function ModelsForm({ status, onValidate, onSave }: Props) {
  const [configuration, setConfiguration] = useState(() => initialModelConfiguration(status));
  const [validation, setValidation] = useState<ModelValidation | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const embeddingLocked = Boolean(status.embedding_change_blocked);
  const change = (next: ModelConfiguration) => {
    setConfiguration(next);
    setValidation(null);
    setMessage("");
  };
  const dimensionsField = (
    <label className="block space-y-1 text-sm">
      Embedding dimensions
      <input
        className={inputClass}
        type="number"
        required
        min={1}
        max={configuration.embedding.provider === "compatible" ? 65535 : 3072}
        step={1}
        disabled={embeddingLocked}
        value={configuration.embedding.dimensions}
        onChange={(event) =>
          change({
            ...configuration,
            embedding: { ...configuration.embedding, dimensions: Number(event.target.value) },
          })
        }
      />
    </label>
  );
  const save = async () => {
    if (busy) return;
    setBusy(true);
    setValidation(null);
    setMessage("");
    try {
      const checked = await onValidate(configuration);
      setValidation(checked);
      if (!checked.ready) return;
      await onSave(configuration);
      setConfiguration((previous) => ({ ...previous, keys: {} }));
      setValidation(null);
      setMessage("Configuration submitted. Check activation and rebuild progress below.");
    } catch {
      setMessage(
        "Configuration was not applied. Check your connection and administrator access, then refresh and try again.",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      className="max-w-3xl space-y-6"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <div>
        <h2 className="text-xl font-semibold">Models &amp; keys</h2>
        <p className="mt-2 text-sm text-[var(--color-text-muted)]">
          Configure the models Loom uses internally. Your Claude Code or Codex login stays separate. Keys stay on the
          server; leaving a key blank keeps its existing value.
        </p>
      </div>
      <fieldset disabled={busy} className="space-y-6 disabled:opacity-70">
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="space-y-1">
            LLM provider
            <select
              className={inputClass}
              value={configuration.llm.provider}
              onChange={(event) => {
                const provider = event.target.value as LlmProvider;
                change({
                  ...configuration,
                  llm: {
                    ...configuration.llm,
                    provider,
                    model: provider === "anthropic" ? "claude-haiku-4-5" : "gpt-6-luna",
                  },
                });
              }}
            >
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic (Claude)</option>
            </select>
          </label>
          <label className="space-y-1">
            LLM model
            <input
              className={inputClass}
              required
              value={configuration.llm.model}
              onChange={(event) =>
                change({ ...configuration, llm: { ...configuration.llm, model: event.target.value } })
              }
            />
          </label>
          <label className="space-y-1">
            Embedding provider
            <select
              className={inputClass}
              disabled={embeddingLocked}
              value={configuration.embedding.provider}
              onChange={(event) => {
                const provider = event.target.value as EmbeddingProvider;
                change({
                  ...configuration,
                  embedding: {
                    provider,
                    model:
                      provider === "compatible"
                        ? ""
                        : provider === "gemini"
                          ? "gemini-embedding-001"
                          : "text-embedding-3-large",
                    dimensions: 3072,
                  },
                });
              }}
            >
              <option value="openai">OpenAI</option>
              <option value="gemini">Google Gemini</option>
              <option value="compatible">Other (OpenAI-compatible API)</option>
            </select>
          </label>
          <label className="space-y-1">
            Embedding model
            <input
              className={inputClass}
              required
              disabled={embeddingLocked}
              value={configuration.embedding.model}
              onChange={(event) =>
                change({ ...configuration, embedding: { ...configuration.embedding, model: event.target.value } })
              }
            />
          </label>
        </div>
        {embeddingLocked && (
          <p role="note" className="text-sm text-[var(--color-text-muted)]">
            {status.embedding_change_blocked}
          </p>
        )}
        {configuration.embedding.provider === "compatible" && (
          <div className="space-y-3">
            <label className="block space-y-1">
              Embedding API base URL
              <input
                className={inputClass}
                type="url"
                required
                placeholder="https://provider.example/v1"
                disabled={embeddingLocked}
                value={configuration.embedding.base_url ?? ""}
                onChange={(event) =>
                  change({ ...configuration, embedding: { ...configuration.embedding, base_url: event.target.value } })
                }
              />
            </label>
            {dimensionsField}
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                disabled={embeddingLocked}
                checked={configuration.embedding.send_dimensions ?? false}
                onChange={(event) =>
                  change({
                    ...configuration,
                    embedding: { ...configuration.embedding, send_dimensions: event.target.checked },
                  })
                }
              />
              Send dimensions parameter (only if the provider supports it)
            </label>
            <p className="text-sm text-[var(--color-text-muted)]">
              Enter the model ID above and its output dimensions here. The endpoint must support the OpenAI embeddings
              request and response format. Use HTTPS for remote services; HTTP sends the key and content unencrypted.
            </p>
          </div>
        )}
        <p className="text-sm text-[var(--color-text-muted)]">
          Claude needs a separate embedding provider. OpenAI can use one API key for both roles. Only keys for your
          selected providers are required.
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          {(Object.keys(PROVIDER_LABELS) as ModelProvider[])
            .filter((provider) => provider !== "compatible" || configuration.embedding.provider === "compatible")
            .map((provider) => (
              <label key={provider} className="space-y-1">
                {PROVIDER_LABELS[provider]} API key
                <input
                  className={inputClass}
                  type="password"
                  // Browsers ignore "off" on password inputs and autofill a saved login here;
                  // "new-password" stops that, the data attributes stop password managers.
                  autoComplete="new-password"
                  data-1p-ignore
                  data-lpignore="true"
                  spellCheck={false}
                  placeholder={status.configured_keys[provider] ? "Configured — leave blank to keep" : "Not configured"}
                  value={configuration.keys[provider] ?? ""}
                  onChange={(event) => {
                    const keys = { ...configuration.keys };
                    if (event.target.value) keys[provider] = event.target.value;
                    else delete keys[provider];
                    change({ ...configuration, keys });
                  }}
                />
              </label>
            ))}
        </div>
        <details className="rounded-lg border border-[var(--color-border)] p-4">
          <summary className="cursor-pointer font-medium">Advanced models per job</summary>
          <p className="my-3 text-sm text-[var(--color-text-muted)]">
            Leave overrides blank to use the main model. Optional tiers stay off unless configured. Cross-provider
            overrides need that provider's key.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            {(Object.entries(MODEL_JOBS) as [keyof typeof MODEL_JOBS, string][]).map(([job, label]) => (
              <label key={job} className="space-y-1 text-sm">
                {label}
                <input
                  className={inputClass}
                  value={configuration.llm.overrides[job] ?? ""}
                  onChange={(event) => {
                    const overrides = { ...configuration.llm.overrides };
                    if (event.target.value.trim()) overrides[job] = event.target.value;
                    else delete overrides[job];
                    change({ ...configuration, llm: { ...configuration.llm, overrides } });
                  }}
                />
              </label>
            ))}
            <label className="space-y-1 text-sm">
              Verification models (comma-separated; blank uses main model)
              <input
                className={inputClass}
                value={configuration.llm.verification_models?.join(", ") ?? ""}
                onChange={(event) =>
                  change({
                    ...configuration,
                    llm: {
                      ...configuration.llm,
                      verification_models: event.target.value.trim()
                        ? event.target.value
                            .split(",")
                            .map((model) => model.trim())
                            .filter(Boolean)
                        : null,
                    },
                  })
                }
              />
            </label>
            {configuration.embedding.provider !== "compatible" && dimensionsField}
          </div>
        </details>
        <p className="text-sm text-[var(--color-text-muted)]">
          Test &amp; apply makes small, billable model requests. Changing the embedding model requires rebuilding
          existing indexes; rotating a key does not.
        </p>
        <div className="flex gap-3">
          <button
            type="submit"
            disabled={busy}
            className="rounded-lg bg-[var(--color-accent)] px-4 py-2 text-[var(--color-on-accent)] disabled:opacity-50"
          >
            Test &amp; apply
          </button>
        </div>
      </fieldset>
      {busy && <p role="status">Checking configuration…</p>}
      {message && <p role="status">{message}</p>}
      {validation && (
        <ul aria-label="Provider checks" className="space-y-2 text-sm">
          {validation.checks.map((check) => (
            <li key={`${check.role}:${check.provider}:${check.model}`}>
              {check.model}: {check.ready ? "Ready" : (errorMessages[check.code] ?? "Provider check failed.")}
              {check.detail && ` (${check.detail})`}
            </li>
          ))}
        </ul>
      )}
    </form>
  );
}
