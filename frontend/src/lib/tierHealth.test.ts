import { describe, expect, it } from "vitest";
import { failureSummary, failingTiers, kindWord, unavailableTiers, type TierHealth } from "./tierHealth";

const row = (over: Partial<TierHealth>): TierHealth => ({
  tier: "clang_full",
  state: "failing",
  kind: "unknown",
  error: "",
  fix: "",
  failing_since: "",
  last_failed_at: "",
  failed_passes: 4,
  last_ok_at: "",
  lag_commits: -1,
  ...over,
});

describe("failureSummary", () => {
  it("says the reason as the health banner does and leaves out a lag of 0", () => {
    expect(failureSummary(row({ kind: "git_lock", lag_commits: 0 }))).toBe("4 failed passes (git lock held)");
    expect(failureSummary(row({ kind: "unknown", lag_commits: 3 }))).toBe(
      "4 failed passes, 3 commits behind (unclassified error)",
    );
  });

  it("reads no kind as unclassified, and a kind it does not know as itself", () => {
    expect(kindWord("")).toBe("unclassified error");
    expect(kindWord("newer_kind")).toBe("newer_kind");
  });
});

describe("unavailableTiers", () => {
  it("lists the tiers that left out a dependency, never as failing", () => {
    const note = row({ state: "unavailable", kind: "dependency_unavailable" });
    const failing = row({ tier: "clang" });
    expect(unavailableTiers([note, failing])).toEqual([note]);
    expect(failingTiers([note, failing])).toEqual([failing]);
    expect(kindWord(note.kind)).toBe("dependency unavailable");
  });
});
