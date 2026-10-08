/**
 * One verdict per code index: converged / N behind / stale / unknown. The
 * fast tier is the headline (it is what search reads); scip lag is
 * secondary. Shared by the repository card and the repository detail's header and Health
 * tab.
 */
const TIER_TOOL: Record<string, string> = {
  clang: "scip-clang",
  typescript: "scip-typescript",
  go: "scip-go",
  python: "scip-python",
};
export type FreshTone = "ok" | "warn" | "bad" | "muted";
export type Fresh = { tone: FreshTone; label: string; detail: string };

export function freshness(r: {
  fast_tier_lag: number;
  scip_lag: number;
  scip_producer?: string;
  indexed_at?: string;
  git_head: string;
}): Fresh {
  if (!r.git_head)
    return { tone: "muted", label: "not indexed", detail: "no git_head watermark — run loom code backfill" };
  const ageMs = r.indexed_at ? Date.now() - new Date(r.indexed_at.replace(" ", "T") + "Z").getTime() : 0;
  if (r.fast_tier_lag > 0) {
    return {
      tone: r.fast_tier_lag > 20 ? "bad" : "warn",
      label: `${r.fast_tier_lag} behind`,
      detail: `${r.fast_tier_lag} merged commit${r.fast_tier_lag === 1 ? "" : "s"} not yet indexed`,
    };
  }
  if (r.fast_tier_lag === 0 && r.scip_lag > 0) {
    // Scip_lag is the entry's own tier; name it when the server says which
    const tier = r.scip_producer ? `${TIER_TOOL[r.scip_producer] ?? r.scip_producer} tier` : "compiler tier";
    return { tone: "warn", label: "graph catching up", detail: `${tier} ${r.scip_lag} commits behind the fast tier` };
  }
  if (r.fast_tier_lag === 0) {
    if (ageMs > 6 * 3600_000)
      return { tone: "warn", label: "quiet", detail: "converged, but no ingest activity in 6h — check the poller" };
    return { tone: "ok", label: "converged", detail: "index == fetched branch tip" };
  }
  return { tone: "muted", label: "unknown", detail: "bare clone unreadable from this replica; watermark shown as-is" };
}
