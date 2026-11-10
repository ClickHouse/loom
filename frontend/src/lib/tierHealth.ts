/**
 * A branch's failing code-index tiers, from the `tier_health` rows `code.repos` returns:
 * each tier's last recorded outcome, with the error kind, since when, how far behind and
 * the one fix while it fails.
 */

export interface TierHealth {
  tier: string;
  state: "ok" | "failing" | "unavailable";
  kind: string;
  error: string;
  fix: string;
  failing_since: string;
  last_failed_at: string;
  failed_passes: number;
  last_ok_at: string;
  lag_commits: number;
}

const TIER_NAME: Record<string, string> = {
  fetch: "Mirror fetch",
  ingest: "Parser tier",
  clang: "C++ compiler tier",
  typescript: "TypeScript compiler tier",
  go: "Go compiler tier",
  python: "Python compiler tier",
  clang_full: "C++ full re-index",
};

const KIND_WORD: Record<string, string> = {
  credential: "no credential",
  pin_missing: "pinned commit gone",
  submodule_url: "relative submodule URL",
  egress: "egress refused",
  network: "host unreachable",
  timeout: "timed out",
  disk: "disk full",
  git_lock: "git lock held",
  file_refused: "file refused",
  permission: "permission denied",
  cmake: "build configure failed",
  harness: "indexer failed",
  module: "module failed to build",
  dependency_unavailable: "dependency unavailable",
  unknown: "unclassified error",
};

export function tierName(tier: string): string {
  return TIER_NAME[tier] ?? `${tier} tier`;
}

export function kindWord(kind: string): string {
  // a kind a later build adds reads as itself
  return KIND_WORD[kind] ?? (kind || "unclassified error");
}

/** The tiers that left out a part they cannot fetch on this host (`unavailable`): a
 *  note with its fix, not a failure. */
export function unavailableTiers(rows: TierHealth[] | undefined): TierHealth[] {
  return (rows ?? []).filter((t) => t.state === "unavailable");
}

/** The failing tiers, in the order the server lists them; [] for a row without any. */
export function failingTiers(rows: TierHealth[] | undefined): TierHealth[] {
  return (rows ?? []).filter((t) => t.state === "failing");
}

/** "12 failed passes, 413 commits behind (no credential)" — without the since time,
 *  which the caller renders as a time element. */
export function failureSummary(t: TierHealth): string {
  const passes = `${t.failed_passes} failed pass${t.failed_passes === 1 ? "" : "es"}`;
  const behind = t.lag_commits > 0 ? `, ${t.lag_commits} commit${t.lag_commits === 1 ? "" : "s"} behind` : "";
  return `${passes}${behind} (${kindWord(t.kind)})`;
}
