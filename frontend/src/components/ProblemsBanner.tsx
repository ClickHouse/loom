import { useEffect, useState } from "react";
import { sharedHealth } from "../lib/sharedReads";
import { Alert } from "./ui/Alert";
import { Badge } from "@clickhouse/click-ui/Badge";
import { IconButton } from "./IconButton";

const STORAGE_KEY = "loom_problems_banner_v1_dismissed";

interface HealthProblem {
  code: string;
  severity: "info" | "warn" | "error";
  message: string;
  fix: string;
  source?: string;
}

/** The well-formed entries of a `/v1/health` body's `problems`; [] when it is absent or
 *  not a list (a proxy's rewrite) — a bad body means no banner, never a
 *  render crash. */
export function healthProblems(d: Record<string, unknown>): HealthProblem[] {
  const raw: unknown = d.problems;
  if (!Array.isArray(raw)) return [];
  return raw.filter(isHealthProblem);
}

function isHealthProblem(p: unknown): p is HealthProblem {
  return (
    typeof p === "object" &&
    p !== null &&
    "code" in p &&
    typeof p.code === "string" &&
    "severity" in p &&
    (p.severity === "info" || p.severity === "warn" || p.severity === "error") &&
    "message" in p &&
    typeof p.message === "string" &&
    "fix" in p &&
    typeof p.fix === "string" &&
    (!("source" in p) || p.source === undefined || typeof p.source === "string")
  );
}

/** The warn and error entries, errors first — the ones worth a banner. */
function visibleProblems(all: HealthProblem[]): HealthProblem[] {
  const rank = { error: 0, warn: 1, info: 2 };
  return all
    .filter((p) => p.severity === "error" || p.severity === "warn")
    .sort((a, b) => rank[a.severity] - rank[b.severity]);
}

/**
 * ProblemsBanner — the `problems` list `/v1/health` carries, rendered on every view: one
 * line per warn/error problem, each with the fix sentence the server attached (an env var
 * or a command). Dismissible for the browser session.
 */
export function ProblemsBanner() {
  const [reported, setReported] = useState<HealthProblem[] | null>(null);
  // The headline is the deployment's `status`: a problem about the data it holds (a
  // repository's failing code-index tier) can be an error without degrading it.
  const [degraded, setDegraded] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    try {
      if (sessionStorage.getItem(STORAGE_KEY) === "1") {
        setDismissed(true);
        return;
      }
    } catch {
      /* */
    }
    sharedHealth()
      .then((d) => {
        if (d) {
          setReported(healthProblems(d));
          setDegraded(d.status === "degraded");
        }
      })
      .catch(() => {
        /* swallow; banner is best-effort */
      });
  }, []);

  if (dismissed || !reported) return null;
  const problems = visibleProblems(reported);
  if (problems.length === 0) return null;

  const dismiss = () => {
    try {
      sessionStorage.setItem(STORAGE_KEY, "1");
    } catch {
      /* */
    }
    setDismissed(true);
  };

  return (
    <div className="px-6 xl:px-8 mt-4 relative" role="status" aria-live="polite" data-testid="problems-banner">
      <Alert
        state={degraded ? "danger" : "warning"}
        size="medium"
        text={
          <div className="pr-8">
            <strong>
              {degraded ? "Loom is degraded" : "Loom is running with warnings"} — {problems.length}{" "}
              {problems.length === 1 ? "problem" : "problems"}.
            </strong>
            <ul className="mt-1.5 space-y-1.5">
              {problems.map((p) => (
                <li key={p.code} className="text-xs" data-severity={p.severity}>
                  <Badge
                    text={p.severity}
                    state={p.severity === "error" ? "danger" : "warning"}
                    size="sm"
                    className="mr-1.5"
                  />
                  <span className="text-text">{p.message}</span> <span className="text-text-muted">Fix: </span>
                  <code className="code-inline text-xs break-words">{p.fix}</code>
                </li>
              ))}
            </ul>
          </div>
        }
      />
      <IconButton
        icon="cross"
        size="sm"
        label="Dismiss for this session"
        onClick={dismiss}
        className="absolute top-2 right-8 xl:right-10"
      />
    </div>
  );
}
