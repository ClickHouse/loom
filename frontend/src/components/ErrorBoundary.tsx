import { Component, type ErrorInfo, type ReactNode } from "react";
import { authHeader } from "../api";
import { isRecord } from "../lib/guards";

/**
 * Root error boundary. Without it a render error anywhere unmounts the whole tree and
 * leaves a blank page with the only explanation in the console; with it the page says
 * something broke, offers Reload, and offers a diagnostics bundle (message, stack,
 * component stack, route, build commit when `/v1/health` answers) the user can paste into
 * a report.
 *
 * Plain markup on purpose: this renders when the app's own components may
 * be the thing that is broken, so it depends on nothing but React.
 */

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
  info: ErrorInfo | null;
  copied: "idle" | "done" | "failed";
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: null, copied: "idle" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ info });
    // The console still gets it — the boundary adds a message, it doesn't hide one.
    console.error("Loom dashboard crashed:", error, info.componentStack);
  }

  private copyDiagnostics = async (): Promise<void> => {
    const text = await buildDiagnostics(this.state.error, this.state.info);
    try {
      await navigator.clipboard.writeText(text);
      this.setState({ copied: "done" });
    } catch {
      this.setState({ copied: "failed" });
    }
  };

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    const { copied } = this.state;
    return (
      <main className="min-h-screen grid place-items-center px-4 bg-[var(--color-bg)] text-[var(--color-text)]">
        <div
          role="alert"
          className="w-full max-w-md rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-6 shadow-sm"
        >
          <h1 className="text-xl font-semibold tracking-tight m-0">Something went wrong</h1>
          <p className="mt-2 text-sm text-[var(--color-text-muted)] leading-relaxed">
            The dashboard hit an error it couldn't recover from. Reloading usually fixes it; if it keeps happening, copy
            the diagnostics and send them to whoever runs this Loom.
          </p>
          <p className="mt-3 text-xs font-mono break-words text-[var(--color-text-muted)]">
            {this.state.error.message || String(this.state.error)}
          </p>
          <div className="mt-5 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => location.reload()}
              className="px-4 py-2 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-lg hover:bg-[var(--color-accent-strong)]"
            >
              Reload
            </button>
            <button
              type="button"
              onClick={this.copyDiagnostics}
              className="px-4 py-2 text-sm font-medium rounded-lg border border-[var(--color-border)] hover:bg-[var(--color-surface-2)]"
            >
              {copied === "done"
                ? "Copied"
                : copied === "failed"
                  ? "Copy failed — select the text above"
                  : "Copy diagnostics"}
            </button>
          </div>
        </div>
      </main>
    );
  }
}

/** The text the Copy button puts on the clipboard. Exported for the test. */
export async function buildDiagnostics(error: Error | null, info: ErrorInfo | null): Promise<string> {
  const lines = [
    `Loom dashboard error — ${new Date().toISOString()}`,
    `route: ${location.pathname}${location.search}${location.hash}`,
    `build: ${await buildCommit()}`,
    `user agent: ${navigator.userAgent}`,
    "",
    `message: ${error?.message ?? String(error)}`,
    "",
    "stack:",
    error?.stack ?? "(none)",
  ];
  if (info?.componentStack) lines.push("", "component stack:", info.componentStack.trim());
  return lines.join("\n");
}

/** `/v1/health` carries `build_commit` (server/main.py); "unknown" when the
 *  server can't be reached in a second or so — a crashed page must not hang
 *  on its own diagnostics. */
async function buildCommit(): Promise<string> {
  try {
    const r = await fetch("/v1/health", { headers: authHeader(), signal: AbortSignal.timeout(1500) });
    if (!r.ok) return "unknown";
    const d: unknown = await r.json();
    return isRecord(d) && typeof d.build_commit === "string" && d.build_commit ? d.build_commit : "unknown";
  } catch {
    return "unknown";
  }
}
