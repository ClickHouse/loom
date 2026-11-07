import { useState } from "react";

/**
 * Runs `reset` during render when `key` changes, so a list keyed to a namespace (or a
 * status) never paints the previous key's rows, and their buttons, under the new one.
 * `reset` sets state, which React applies before this render commits.
 */
export function useResetOnKey(key: string, reset: () => void): void {
  const [seen, setSeen] = useState(key);
  if (seen !== key) {
    setSeen(key);
    reset();
  }
}
