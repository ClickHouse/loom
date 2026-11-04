import { useCallback, useEffect, useRef, useState } from "react";
import { humanizeError } from "../../lib/format";

export interface ScopedRead<T> {
  data: T | null;
  error: string | null;
  busy: boolean;
  /** Start the read; a newer one aborts it, and an answer for a scope the page left is dropped. */
  run: (read: (signal: AbortSignal) => Promise<T>) => void;
}

type State<T> = { scope: string; data: T | null; error: string | null; busy: boolean };

/** One on-demand read whose answer belongs to `scope`. The state carries the scope it was
 *  read for, so the first render under a new scope already shows nothing of the old one. */
export function useScopedRead<T>(scope: string): ScopedRead<T> {
  const [state, setState] = useState<State<T>>({
    scope,
    data: null,
    error: null,
    busy: false,
  });
  const controller = useRef<AbortController | null>(null);

  // Leaving a scope aborts the read still in flight for it, and that scope stops being
  // busy: coming back shows what was there before, not a loader that never ends.
  useEffect(
    () => () => {
      controller.current?.abort();
      controller.current = null;
      setState((s) => (s.scope === scope ? { ...s, busy: false } : s));
    },
    [scope],
  );

  const run = useCallback(
    (read: (signal: AbortSignal) => Promise<T>) => {
      controller.current?.abort();
      const mine = new AbortController();
      controller.current = mine;
      setState((s) =>
        s.scope === scope ? { ...s, error: null, busy: true } : { scope, data: null, error: null, busy: true },
      );
      // A read that was replaced, or whose scope the page left, leaves the state alone.
      const settle = (next: (s: State<T>) => State<T>) => {
        if (controller.current !== mine) return;
        controller.current = null;
        setState(next);
      };
      read(mine.signal)
        .then((data) => settle(() => ({ scope, data, error: null, busy: false })))
        .catch((e) =>
          settle((s) => ({ scope, data: s.scope === scope ? s.data : null, error: humanizeError(e), busy: false })),
        );
    },
    [scope],
  );

  if (state.scope !== scope) return { data: null, error: null, busy: false, run };
  return { data: state.data, error: state.error, busy: state.busy, run };
}
