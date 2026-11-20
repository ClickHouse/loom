import { describe, expect, it } from "vitest";
import { freshness, lagLabel } from "./codeFreshness";

const at = (hoursAgo: number) => new Date(Date.now() - hoursAgo * 3600_000).toISOString();
const base = { fast_tier_lag: 0, scip_lag: 0, git_head: "abc123", indexed_at: at(1) };

// The repository header's tooltip: what the reader sees, in plain words, for each state.
describe("freshness", () => {
  it.each(["not_member", "token_scope", "closed", undefined] as const)(
    "requires access before interpreting redacted metadata (%s)",
    (reason) => {
      const redacted = { ...base, readable: false, unreadable_reason: reason, git_head: "", indexed_at: "" };
      expect(freshness(redacted)).toMatchObject({
        label: "Access required",
        tone: "muted",
        detail:
          reason === "token_scope"
            ? "Index status is hidden because this token does not include the repository's namespace."
            : "Read access is required to view index status.",
      });
      expect(freshness({ ...redacted, stage: "error" }, "Clone failed").label).toBe("Access required");
      for (const stage of ["queued", "cloning", "indexing", "embedding"])
        expect(freshness({ ...redacted, stage }).label).toBe("Access required");
      expect(freshness({ ...redacted, git_head: "abc123", fast_tier_lag: 42 }).label).toBe("Access required");
    },
  );

  it("keeps an explicitly readable empty repository unindexed", () => {
    const empty = { ...base, readable: true, git_head: "", indexed_at: "" };
    expect(freshness(empty)).toMatchObject({ label: "Not indexed", tone: "muted" });
  });

  it.each(["queued", "cloning", "indexing", "embedding"])(
    "shows a readable first index in progress before a completed head exists (%s)",
    (stage) => {
      const building = { ...base, readable: true, git_head: "", indexed_at: "", stage };
      expect(freshness(building)).toMatchObject({ label: "Indexing", tone: "info" });
      expect(freshness({ ...building, git_head: base.git_head }).label).toBe("Indexing");
      expect(freshness(building).detail).toContain("in progress");
      expect(freshness(building, "First index in progress. Search works on what is indexed so far.").detail).toBe(
        "First index in progress. Search works on what is indexed so far.",
      );
    },
  );

  it.each(["", "ready"])("keeps a readable empty repository without active work unindexed (%s)", (stage) => {
    expect(freshness({ ...base, readable: true, git_head: "", indexed_at: "", stage }).label).toBe("Not indexed");
  });

  it("says the first index has not finished, and who runs it for a configured repository", () => {
    expect(freshness({ ...base, git_head: "" }).detail).toBe(
      "The first index hasn't finished yet; for an operator-configured repository, an operator runs it",
    );
  });

  it("counts the merged commits search has not indexed", () => {
    expect(freshness({ ...base, fast_tier_lag: 3 })).toMatchObject({ label: "Behind 3 commits", tone: "warn" });
    expect(freshness({ ...base, fast_tier_lag: 1200 }).label).toBe("Behind 1,200 commits");
  });

  it("says search matches the last fetched commit while the compiler tier is behind it", () => {
    const f = freshness({ ...base, scip_lag: 1 });
    expect(f.detail).toBe("Search matches the last commit Loom fetched; exact cross-file calls are 1 commit behind it");
  });

  it("names an operator's step when nothing was indexed in 6 hours", () => {
    const f = freshness({ ...base, indexed_at: at(7) });
    expect(f.label).toBe("No recent indexing");
    expect(f.detail).toContain("an operator of this deployment can check the repository's sync");
  });

  it("says the index matches the last fetched commit when it does", () => {
    expect(freshness(base).detail).toBe("The index matches the last commit Loom fetched from the branch");
  });

  it("says how far behind can't be told when the server answers no lag", () => {
    expect(freshness({ ...base, fast_tier_lag: -1 }).detail).toBe(
      "How far the index is behind the branch can't be told from this server",
    );
  });

  it("says a first index failed rather than still running", () => {
    expect(freshness({ readable: true, fast_tier_lag: 0, scip_lag: 0, git_head: "", stage: "error" })).toMatchObject({
      label: "Index failed",
      tone: "bad",
    });
    expect(
      freshness({ fast_tier_lag: 0, scip_lag: 0, git_head: "", stage: "error" }, "The clone was refused.").detail,
    ).toBe("The first index failed. The clone was refused.");
  });

  describe("lagLabel", () => {
    it("lets stale win over a zero lag", () => {
      expect(lagLabel(0, true)).toBe("Stale");
      expect(lagLabel(3, true)).toBe("Stale · behind 3 commits");
      expect(lagLabel(0, false)).toBe("Up to date");
      expect(lagLabel(1, false)).toBe("Behind 1 commit");
      expect(lagLabel(-1, false)).toBe("Freshness unknown");
    });
  });
});
