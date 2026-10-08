import { useEffect, useState } from "react";
import { sharedHealth } from "../lib/sharedReads";
import { AlertTriangle, OctagonAlert, X as XIcon } from "lucide-react";

const STORAGE_KEY = "loom_problems_banner_v1_dismissed";

interface HealthProblem {
  code: string;
  severity: "info" | "warn" | "error";
  message: string;
  fix: string;
  source?: string;
}

/** The well-formed entries of a `/v1/health` body's `problems`; [] when it is absent or
 *  not a list (an older server, a proxy's rewrite) — a bad body means no banner, never a
 *  render crash. */
function healthProblems(d: Record<string, unknown>): HealthProblem[] {
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
        if (d) setReported(healthProblems(d));
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

  const hasError = problems.some((p) => p.severity === "error");
  const tone = hasError ? "var(--color-danger, var(--color-warn))" : "var(--color-warn)";
  const Icon = hasError ? OctagonAlert : AlertTriangle;

  return (
    <div className="px-6 xl:px-8 mt-4" role="status" aria-live="polite" data-testid="problems-banner">
      <div
        className="rounded-lg border px-4 py-3 flex items-start gap-3"
        style={{
          borderColor: `color-mix(in srgb, ${tone} 40%, transparent)`,
          background: `color-mix(in srgb, ${tone} 10%, transparent)`,
        }}
      >
        <Icon size={16} className="mt-0.5 shrink-0" style={{ color: tone }} aria-hidden="true" />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-[var(--color-text)]">
            <strong>
              {hasError ? "Loom is degraded" : "Loom is running with warnings"} — {problems.length}{" "}
              {problems.length === 1 ? "problem" : "problems"}.
            </strong>
          </div>
          <ul className="mt-1.5 space-y-1.5">
            {problems.map((p) => (
              <li key={p.code} className="text-xs leading-relaxed" data-severity={p.severity}>
                <span className="font-mono text-2xs uppercase mr-1.5" style={{ color: tone }}>
                  {p.severity}
                </span>
                <span className="text-[var(--color-text)]">{p.message}</span>{" "}
                <span className="text-[var(--color-text-muted)]">Fix: </span>
                <code className="font-mono text-2xs bg-[var(--color-surface-2)] px-1 py-0.5 rounded break-words">
                  {p.fix}
                </code>
              </li>
            ))}
          </ul>
        </div>
        <button
          onClick={dismiss}
          aria-label="Dismiss for this session"
          title="Dismiss for this session"
          className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded shrink-0"
        >
          <XIcon size={13} />
        </button>
      </div>
    </div>
  );
}
