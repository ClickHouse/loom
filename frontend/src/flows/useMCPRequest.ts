import { useCallback, useEffect, useRef, useState } from "react";

/** One abortable request lane. A caller or draft change rejects even an abort-ignoring answer. */
export function useMCPRequest(scope: string) {
  const current = useRef(scope);
  current.current = scope;
  const pending = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    setBusy(false);
    setError(null);
    return () => { pending.current?.abort(); };
  }, [scope]);
  const run = useCallback(async <T,>(work: (signal: AbortSignal) => Promise<T>): Promise<T | undefined> => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    const stamp = current.current;
    setBusy(true);
    setError(null);
    try {
      const result = await work(controller.signal);
      if (!controller.signal.aborted && current.current === stamp) return result;
    } catch (failure) {
      if (!controller.signal.aborted && current.current === stamp) setError(failure);
    } finally {
      if (!controller.signal.aborted && current.current === stamp) setBusy(false);
    }
    return undefined;
  }, []);
  return { run, busy, error };
}
