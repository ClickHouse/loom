// An open tab notices a new build.
//
// A single-page app keeps its JavaScript until the tab reloads, so every fix waits on
// people remembering to refresh. The page already knows the server's build hash from
// /v1/health; this hook remembers the first one it sees and reports when a later poll
// differs.
import { useCallback, useEffect, useRef, useState } from "react";
import { useVisiblePoll } from "./hooks";
import { sharedHealth } from "./sharedReads";

export const BUILD_POLL_MS = 5 * 60_000;
/** The first check waits until the page has settled: it shares the problems
 *  banner's health read when both run within a minute, and it must never sit
 *  on the boot path. */
export const BUILD_FIRST_CHECK_MS = 8_000;

async function fetchBuild(): Promise<string | null> {
  try {
    const j = await sharedHealth();
    const b = j?.build_commit;
    return typeof b === "string" && b ? b : null;
  } catch {
    return null;
  }
}

/** The new build hash once the server's build differs from the one this
 *  tab booted against; null until then. Unknown/unreachable answers never
 *  count as a change. */
export function useBuildWatch(enabled = true): string | null {
  const booted = useRef<string | null>(null);
  const [newBuild, setNewBuild] = useState<string | null>(null);
  const check = useCallback(async () => {
    const b = await fetchBuild();
    if (!b) return;
    if (booted.current === null) {
      booted.current = b;
      return;
    }
    if (b !== booted.current) setNewBuild(b);
  }, []);
  useEffect(() => {
    if (!enabled) return;
    const t = setTimeout(() => {
      void check();
    }, BUILD_FIRST_CHECK_MS);
    return () => clearTimeout(t);
  }, [enabled, check]);
  useVisiblePoll(
    () => {
      void check();
    },
    BUILD_POLL_MS,
    enabled && newBuild === null,
  );
  return newBuild;
}
