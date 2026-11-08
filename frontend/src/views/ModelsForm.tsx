import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { NumberField } from "@clickhouse/click-ui/NumberField";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  LOCAL_MODELS,
  MODEL_JOBS,
  type EmbeddingProvider,
  type LlmProvider,
  type ModelConfiguration,
  type ModelProvider,
  type ModelStatus,
  type ModelValidation,
} from "../api/models";
import { Loading } from "../components/ui";

const PROVIDER_LABELS = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  gemini: "Google Gemini",
  compatible: "Custom embedding provider",
};
const errorMessages: Record<string, string> = {
  missing_key: "Add an API key for this provider.",
  invalid_credentials: "The provider rejected the key. Check its permissions and try again.",
  quota_or_rate_limit: "Check provider billing or quota, then try again.",
  model_unavailable: "This model is not available to your API account.",
  timeout: "The provider did not respond in time. Try again.",
  invalid_output: "The model response did not match the expected format or dimensions.",
  unsupported_dimensions: "The vector tables need a dimension count that is a multiple of 8.",
  unsupported_provider: "This provider is not supported for this job.",
  gateway_required:
    "Model calls must go through the inference gateway: use a base URL at a host in LOOM_INFERENCE_GATEWAY_HOSTS, or an OpenAI-compatible provider at the gateway.",
  provider_error: "The provider check failed. Check the model and provider availability.",
};

export function initialModelConfiguration(status: ModelStatus): ModelConfiguration {
  const llm = status.llm_provider === "anthropic" ? "anthropic" : "openai";
  const embedding: EmbeddingProvider =
    status.embedding_provider === "compatible" || status.embedding_provider === "gemini"
      ? status.embedding_provider
      : status.embedding_provider === "local" && status.embedding_model in LOCAL_MODELS
        ? "local"
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
        status.embedding_provider === embedding && status.embedding_model
          ? status.embedding_model
          : embedding === "gemini"
            ? "gemini-embedding-001"
            : "text-embedding-3-large",
      dimensions: status.embedding_provider === embedding ? status.embedding_dimensions : 3072,
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
    <NumberField
      label="Embedding dimensions"
      required
      min={1}
      max={configuration.embedding.provider === "compatible" ? 65535 : 3072}
      step={1}
      disabled={embeddingLocked}
      value={configuration.embedding.dimensions}
      onChange={(value) =>
        change({
          ...configuration,
          embedding: { ...configuration.embedding, dimensions: Number(value) },
        })
      }
    />
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
        <h2>Models &amp; keys</h2>
        <p className="mt-2 text-sm text-text-muted">
          Configure the models Loom uses internally. Your Claude Code or Codex login stays separate. Keys stay on the
          server; leaving a key blank keeps its existing value.
        </p>
      </div>
      <fieldset disabled={busy} className="space-y-6">
        <div className="grid gap-4 sm:grid-cols-2">
          <Select
            label="LLM provider"
            value={configuration.llm.provider}
            onSelect={(value) => {
              const provider = value as LlmProvider;
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
            <Select.Item value="openai">OpenAI</Select.Item>
            <Select.Item value="anthropic">Anthropic (Claude)</Select.Item>
          </Select>
          <TextField
            label="LLM model"
            required
            value={configuration.llm.model}
            onChange={(value) => change({ ...configuration, llm: { ...configuration.llm, model: value } })}
          />
          <Select
            label="Embedding provider"
            disabled={embeddingLocked}
            value={configuration.embedding.provider}
            onSelect={(value) => {
              const provider = value as EmbeddingProvider;
              change({
                ...configuration,
                embedding: {
                  provider,
                  model:
                    provider === "local"
                      ? "local"
                      : provider === "compatible"
                        ? ""
                        : provider === "gemini"
                          ? "gemini-embedding-001"
                          : "text-embedding-3-large",
                  dimensions: provider === "local" ? LOCAL_MODELS.local.dimensions : 3072,
                },
              });
            }}
          >
            <Select.Item value="openai">OpenAI</Select.Item>
            <Select.Item value="gemini">Google Gemini</Select.Item>
            <Select.Item value="compatible">Other (OpenAI-compatible API)</Select.Item>
            <Select.Item value="local">Local (on this server, no key)</Select.Item>
          </Select>
          {configuration.embedding.provider === "local" ? (
            <Select
              label="Embedding model"
              disabled={embeddingLocked}
              value={configuration.embedding.model}
              onSelect={(value) => {
                const model = value as keyof typeof LOCAL_MODELS;
                change({
                  ...configuration,
                  embedding: { provider: "local", model, dimensions: LOCAL_MODELS[model].dimensions },
                });
              }}
            >
              {(Object.keys(LOCAL_MODELS) as (keyof typeof LOCAL_MODELS)[]).map((model) => (
                <Select.Item key={model} value={model}>
                  {`${LOCAL_MODELS[model].label}, ${LOCAL_MODELS[model].dimensions} dimensions`}
                </Select.Item>
              ))}
            </Select>
          ) : (
            <TextField
              label="Embedding model"
              required
              disabled={embeddingLocked}
              value={configuration.embedding.model}
              onChange={(value) =>
                change({ ...configuration, embedding: { ...configuration.embedding, model: value } })
              }
            />
          )}
        </div>
        {embeddingLocked && (
          <p role="note" className="text-sm text-text-muted">
            {status.embedding_change_blocked}
          </p>
        )}
        {configuration.embedding.provider === "compatible" && (
          <div className="space-y-3">
            <TextField
              label="Embedding API base URL"
              type="url"
              required
              placeholder="https://provider.example/v1"
              disabled={embeddingLocked}
              value={configuration.embedding.base_url ?? ""}
              onChange={(value) =>
                change({ ...configuration, embedding: { ...configuration.embedding, base_url: value } })
              }
            />
            {dimensionsField}
            <Checkbox
              label="Send dimensions parameter (only if the provider supports it)"
              disabled={embeddingLocked}
              checked={configuration.embedding.send_dimensions ?? false}
              onCheckedChange={(checked) =>
                change({
                  ...configuration,
                  embedding: { ...configuration.embedding, send_dimensions: checked === true },
                })
              }
            />
            <p className="text-sm text-text-muted">
              Enter the model ID above and its output dimensions here. The endpoint must support the OpenAI embeddings
              request and response format. Use HTTPS for remote services; HTTP sends the key and content unencrypted.
            </p>
          </div>
        )}
        <p className="text-sm text-text-muted">
          Claude needs a separate embedding provider. OpenAI can use one API key for both roles. Only keys for your
          selected providers are required.
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          {(Object.keys(PROVIDER_LABELS) as ModelProvider[])
            .filter((provider) => provider !== "compatible" || configuration.embedding.provider === "compatible")
            .map((provider) => (
              <PasswordField
                key={provider}
                label={`${PROVIDER_LABELS[provider]} API key`}
                // Browsers ignore "off" on password inputs and autofill a saved login here;
                // "new-password" stops that, the data attributes stop password managers.
                autoComplete="new-password"
                data-1p-ignore
                data-lpignore="true"
                spellCheck={false}
                placeholder={status.configured_keys[provider] ? "Configured — leave blank to keep" : "Not configured"}
                value={configuration.keys[provider] ?? ""}
                onChange={(value) => {
                  const keys = { ...configuration.keys };
                  if (value) keys[provider] = value;
                  else delete keys[provider];
                  change({ ...configuration, keys });
                }}
              />
            ))}
        </div>
        <details className="rounded-lg border border-border p-4">
          <summary className="cursor-pointer font-medium">Advanced models per job</summary>
          <p className="my-3 text-sm text-text-muted">
            Leave overrides blank to use the main model. Optional tiers stay off unless configured. Cross-provider
            overrides need that provider's key.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            {(Object.entries(MODEL_JOBS) as [keyof typeof MODEL_JOBS, string][]).map(([job, label]) => (
              <TextField
                key={job}
                label={label}
                value={configuration.llm.overrides[job] ?? ""}
                onChange={(value) => {
                  const overrides = { ...configuration.llm.overrides };
                  if (value.trim()) overrides[job] = value;
                  else delete overrides[job];
                  change({ ...configuration, llm: { ...configuration.llm, overrides } });
                }}
              />
            ))}
            <TextField
              label="Verification models (comma-separated; blank uses main model)"
              value={configuration.llm.verification_models?.join(", ") ?? ""}
              onChange={(value) =>
                change({
                  ...configuration,
                  llm: {
                    ...configuration.llm,
                    verification_models: value.trim()
                      ? value
                          .split(",")
                          .map((model) => model.trim())
                          .filter(Boolean)
                      : null,
                  },
                })
              }
            />
            {!["compatible", "local"].includes(configuration.embedding.provider) && dimensionsField}
          </div>
        </details>
        <p className="text-sm text-text-muted">
          Test &amp; apply makes small, billable model requests. Changing the embedding model requires rebuilding
          existing indexes; rotating a key does not.
        </p>
        <div className="flex justify-end">
          <Button type="primary" htmlType="submit" loading={busy}>
            Test &amp; apply
          </Button>
        </div>
      </fieldset>
      {busy && <Loading layout="inline" label="Checking the configuration" />}
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
