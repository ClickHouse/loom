import { useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import {
  getModelStatus,
  resetModelConfiguration,
  retryModelConfiguration,
  saveModelConfiguration,
  validateModelConfiguration,
  type ModelStatus,
} from "../api/models";
import { ModelsForm } from "./ModelsForm";

const PHASES: Record<string, string> = {
  drain: "Pausing agent requests",
  snapshot: "Copying stored data",
  rebuild: "Re-embedding stored data",
  verify: "Checking the new vectors",
  publish: "Switching to the new vectors",
  cleanup: "Removing temporary copies",
  activate: "Turning on the new model",
};
const ORDER = Object.keys(PHASES);

function RebuildProgress({ pending }: { pending: NonNullable<ModelStatus["pending"]> }) {
  const { phase, rows_done: rows, table_index: index, table_count: tables } = pending.progress;
  const step = typeof phase === "string" ? ORDER.indexOf(phase) : -1;
  const failed = pending.state === "failed";
  return (
    <>
      {step >= 0 && (
        <p>
          Step {step + 1} of {ORDER.length}: {PHASES[phase as string]}
          {(phase === "snapshot" || phase === "rebuild") && typeof index === "number" && typeof tables === "number"
            ? ` (table ${Math.min(index + 1, tables)} of ${tables})`
            : ""}
          {typeof rows === "number" && rows > 0 ? `, ${rows.toLocaleString()} rows processed` : ""}.
        </p>
      )}
      {failed && (
        <p>
          The rebuild stopped{step >= 0 ? ` at “${PHASES[phase as string].toLowerCase()}”` : ""}. Agent requests stay
          paused and stored data is kept. Retry resumes from the last checkpoint.
        </p>
      )}
      <details className="mt-2 text-sm text-text-muted">
        <summary>Technical details</summary>
        {pending.error_code && <p>error: {pending.error_code}</p>}
        {Object.entries(pending.progress).map(([name, value]) => (
          <p key={name}>
            {name}: {value}
          </p>
        ))}
      </details>
    </>
  );
}

export function ModelsSettings() {
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [resetting, setResetting] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [retrying, setRetrying] = useState(false);
  // Outlives the form, which remounts on the new version.
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let loading = false;
    const load = async () => {
      if (loading || controller.signal.aborted) return;
      loading = true;
      try {
        const next = await getModelStatus(controller.signal);
        if (!controller.signal.aborted) {
          setStatus(next);
          setError("");
        }
      } catch {
        if (!controller.signal.aborted)
          setError(
            "Could not load model settings. Platform administrator access is required on authenticated servers.",
          );
      } finally {
        loading = false;
      }
    };
    void load();
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 3000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [reload]);
  if (!status) return <p role="status">{error || "Loading model settings…"}</p>;
  return (
    <div className="space-y-5">
      <p className="text-sm text-text-muted">
        Deployment-wide configuration · Source:{" "}
        {status.source === "dashboard" ? "saved dashboard settings" : "server environment"}. Saved dashboard settings
        take precedence over environment defaults.
      </p>
      {error && <p role="alert">{error}</p>}
      {status.source === "dashboard" && !status.pending && (
        <div className="rounded-lg border border-border p-4">
          {confirmReset ? (
            <>
              <p>
                Remove dashboard overrides and use this server's startup environment? If the embedding space changes,
                Loom must rebuild before switching. This does not reload a changed .env file.
              </p>
              <div className="mt-3 flex flex-wrap justify-end gap-2">
                <Button type="secondary" htmlType="button" disabled={resetting} onClick={() => setConfirmReset(false)}>
                  Cancel
                </Button>
                <Button
                  type="primary"
                  htmlType="button"
                  loading={resetting}
                  onClick={async () => {
                    if (resetting) return;
                    setResetting(true);
                    try {
                      await resetModelConfiguration(status.version);
                      setConfirmReset(false);
                      setReload((value) => value + 1);
                    } catch {
                      setError("Could not reset model settings. Refresh and try again.");
                    } finally {
                      setResetting(false);
                    }
                  }}
                >
                  Confirm environment configuration
                </Button>
              </div>
            </>
          ) : (
            <Button type="secondary" htmlType="button" onClick={() => setConfirmReset(true)}>
              Use environment configuration
            </Button>
          )}
        </div>
      )}
      {status.pending ? (
        <div role="status" className="rounded-lg border border-border p-4">
          <h2 className="font-semibold">Embedding configuration: {status.pending.state}</h2>
          <p>The new embedding model is not active until its rebuild completes. Existing source data is retained.</p>
          <RebuildProgress pending={status.pending} />
          {status.pending.state === "failed" && (
            <Button
              type="primary"
              htmlType="button"
              loading={retrying}
              className="mt-3"
              onClick={async () => {
                if (!status.pending || retrying) return;
                setRetrying(true);
                try {
                  await retryModelConfiguration(status.pending.config_id);
                  setReload((value) => value + 1);
                } catch {
                  setError("Could not retry the rebuild. Refresh and try again.");
                } finally {
                  setRetrying(false);
                }
              }}
            >
              Retry rebuild
            </Button>
          )}
        </div>
      ) : (
        <>
          {saved && <p role="status">Saved. Loom is using the new configuration.</p>}
          <ModelsForm
            key={status.version}
            status={status}
            onValidate={(configuration) => {
              setSaved(false);
              return validateModelConfiguration(configuration);
            }}
            onSave={async (configuration) => {
              await saveModelConfiguration(configuration);
              setSaved(true);
              setReload((value) => value + 1);
            }}
          />
        </>
      )}
    </div>
  );
}
