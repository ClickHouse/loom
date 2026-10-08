import { useEffect, useState } from "react";
import { subscribeInflight } from "../api";

/**
 * Thin progress bar pinned to the top of the viewport.
 *
 * Shows whenever there's at least one in-flight POST to /v1/*. Uses a
 * pure-CSS indeterminate animation (no React re-render during the
 * animation) so it never adds work to the main thread while requests
 * are running. Visual mirror of the bar GitHub/Vercel/CH-docs show on
 * page transitions — gives the eye something to track during the
 * 100-500ms window where data is loading.
 */
export function NetworkProgress() {
  const [busy, setBusy] = useState(false);
  useEffect(() => subscribeInflight((n) => setBusy(n > 0)), []);

  return (
    <div
      aria-hidden="true"
      className={`fixed top-0 left-0 right-0 h-[2px] z-[60] pointer-events-none transition-opacity duration-150 ${
        busy ? "opacity-100" : "opacity-0"
      }`}
    >
      <div className="h-full bg-[var(--color-accent)]/30 overflow-hidden">
        <div className="h-full w-1/3 bg-[var(--color-accent)] animate-network-slide motion-reduce:animate-none!" />
      </div>
    </div>
  );
}
