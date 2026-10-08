import { RefreshCw } from "lucide-react";
import { useBuildWatch } from "../lib/buildWatch";

/** "A new version is live — Reload." Shown once the server's build differs
 * from the one this tab booted against. */
export function ReloadBar({ enabled = true }: { enabled?: boolean }) {
  const newBuild = useBuildWatch(enabled);
  if (!newBuild) return null;
  return (
    <div
      role="status"
      data-testid="reload-bar"
      className="flex items-center justify-center gap-3 px-4 py-2 text-sm bg-[var(--color-accent)] text-[var(--color-on-accent)]"
    >
      <span>A new version of Loom is live (build {newBuild.slice(0, 9)}). Reload to pick it up.</span>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md bg-[var(--color-surface)] text-[var(--color-text)] font-medium hover:bg-[var(--color-surface-2)]"
      >
        <RefreshCw size={12} aria-hidden="true" /> Reload
      </button>
    </div>
  );
}
