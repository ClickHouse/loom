// What the current view asked the server — how many /v1 calls, and the span from the
// first request's start to the last response's end. `api.ts` reports every call here;
// `vitals.ts` reads and resets the counters per view. Kept apart from both so neither
// imports the other's module.
let calls = 0;
let firstStart = 0;
let lastEnd = 0;

export function noteCallStart(): void {
  const t = performance.now();
  if (calls === 0) firstStart = t;
  calls += 1;
}

export function noteCallEnd(): void {
  lastEnd = performance.now();
}

export function readCalls(): { api_calls: number; api_ms: number } {
  // The span is capped at a day — the server's ceiling — so a tab left open never loses
  // its row to validation.
  const span = calls > 0 && lastEnd > firstStart ? Math.round(lastEnd - firstStart) : 0;
  return { api_calls: calls, api_ms: Math.min(span, 86_400_000) };
}

export function resetCalls(): void {
  calls = 0;
  firstStart = 0;
  lastEnd = 0;
}
