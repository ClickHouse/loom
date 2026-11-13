import { fmtInt, parseChTime } from "./format";
export type FreshTone = "ok" | "warn" | "bad" | "muted";
export type Fresh = { tone: FreshTone; label: string; detail: string };

/** The words for an answer's index lag (Explore's badge): stale wins, so a stale index whose
 *  mirror has not fetched (lag 0) never reads "Up to date". */
export function lagLabel(lag: number, stale: boolean): string {
  const commits = `${fmtInt(lag)} commit${lag === 1 ? "" : "s"}`;
  if (stale) return lag > 0 ? `Stale · behind ${commits}` : "Stale";
  if (lag < 0) return "Freshness unknown";
  return lag === 0 ? "Up to date" : `Behind ${commits}`;
}

/**
 * One verdict per code index: not indexed, index failed, up to date, behind N commits, calls
 * catching up, no recent indexing, or unknown. Search's lag is the headline; the compiler
 * tier's is secondary. Shared by the repository detail's header and a branch's Behind line
 * on the repository card.
 */
export function freshness(
  r: {
    fast_tier_lag: number;
    scip_lag: number;
    indexed_at?: string;
    git_head: string;
    stage?: string;
  },
  /** Why a failed first index failed, as the branch card words it (`stageLabel`). */
  failure?: string,
): Fresh {
  if (!r.git_head && r.stage === "error")
    return {
      tone: "bad",
      label: "Index failed",
      detail: failure ? `The first index failed. ${failure}` : "The first index failed",
    };
  if (!r.git_head)
    return {
      tone: "muted",
      label: "Not indexed",
      detail: "The first index hasn't finished yet; for an operator-configured repository, an operator runs it",
    };
  const indexed = parseChTime(r.indexed_at);
  const ageMs = indexed ? Date.now() - indexed.getTime() : 0;
  if (r.fast_tier_lag > 0) {
    return {
      tone: r.fast_tier_lag > 20 ? "bad" : "warn",
      label: `Behind ${fmtInt(r.fast_tier_lag)} commit${r.fast_tier_lag === 1 ? "" : "s"}`,
      detail: `${fmtInt(r.fast_tier_lag)} merged commit${r.fast_tier_lag === 1 ? "" : "s"} not yet indexed`,
    };
  }
  if (r.fast_tier_lag === 0 && r.scip_lag > 0) {
    // The fast tier is at the last fetched commit here, and the compiler tier's lag is counted from it.
    return {
      tone: "warn",
      label: "Calls catching up",
      detail: `Search matches the last commit Loom fetched; exact cross-file calls are ${fmtInt(r.scip_lag)} commit${r.scip_lag === 1 ? "" : "s"} behind it`,
    };
  }
  if (r.fast_tier_lag === 0) {
    if (ageMs > 6 * 3600_000)
      return {
        tone: "warn",
        label: "No recent indexing",
        detail:
          "The index matches the last commit Loom fetched, but nothing was indexed in 6 hours; if the branch has newer commits, they aren't being picked up: Refresh now on the repository's Health tab fetches the branch, and an operator of this deployment can check the repository's sync",
      };
    return {
      tone: "ok",
      label: "Up to date",
      detail: "The index matches the last commit Loom fetched from the branch",
    };
  }
  return {
    tone: "muted",
    label: "Freshness unknown",
    detail: "How far the index is behind the branch can't be told from this server",
  };
}
