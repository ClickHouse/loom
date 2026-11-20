import type { AssistantReason } from "../../lib/assistantLayout";
import { isRecord } from "../../lib/guards";

export type AssistantCheck = "ready" | "not_configured" | "starting" | "failing";

/** How long a readiness read may take before it counts as failed. */
export const READINESS_TIMEOUT_MS = 10_000;

/** `/v1/health/ready`'s `assistant` check (server/core/readiness.py), never cached, its body
 *  read on a 503 as on a 200. The check is registered only where an assistant is configured,
 *  so a missing one means none is; null when the read failed, took over
 *  READINESS_TIMEOUT_MS, or the body is not readiness. */
export async function readAssistantCheck(signal?: AbortSignal): Promise<AssistantCheck | null> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), READINESS_TIMEOUT_MS);
  const stop = () => ctl.abort();
  signal?.addEventListener("abort", stop, { once: true });
  if (signal?.aborted) ctl.abort();
  try {
    const r = await fetch("/v1/health/ready", { cache: "no-store", signal: ctl.signal });
    const body: unknown = await r.json();
    const checks = isRecord(body) && isRecord(body.checks) ? body.checks : null;
    if (!checks) return null;
    const a = checks.assistant;
    if (a === undefined) return "not_configured";
    if (!isRecord(a)) return null;
    if (a.ok === true) return "ready";
    return a.reason === "starting" ? "starting" : "failing";
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", stop);
  }
}

/** The reason readiness gives the panel: none when the assistant is ready or the read
 *  failed (the chat route's refusal then decides). */
export async function readReadiness(signal?: AbortSignal): Promise<AssistantReason | null> {
  const check = await readAssistantCheck(signal);
  return check === null || check === "ready" ? null : { kind: check };
}
