import { describe, expect, it } from "vitest";
import type { MemoryHistoryEntry } from "../api/admin";
import { historyChange } from "./historyChange";

const base: MemoryHistoryEntry = {
  version: 1,
  value: "same text",
  created_at: "2026-01-01T00:00:00Z",
  tombstoned_at: null,
  redacted_at: null,
  source_trace: "",
  confidence: 0.75,
  status: "active",
  conflict_with: [],
  tags: ["kind:observation"],
  verified_at: null,
  lock_mode: "",
};

describe("historyChange", () => {
  it("names a conflict mark with the value unchanged", () => {
    const h = { ...base, version: 2, status: "conflicted", conflict_with: ["a", "b", "c"] };
    expect(historyChange(h, base)).toEqual(["marked conflicted · 3 memories"]);
  });

  it("names tag, verification, lock and pin changes", () => {
    const h = {
      ...base,
      version: 2,
      tags: ["kind:observation", "owner:new"],
      verified_at: "2026-01-02T00:00:00Z",
      lock_mode: "frozen",
      pinned: true,
    };
    expect(historyChange(h, base)).toEqual(["tags +owner:new", "verified", "locked frozen", "pinned"]);
    expect(historyChange(base, h)).toEqual(["tags −owner:new", "verification revoked", "unlocked", "unpinned"]);
  });

  it("says nothing for the oldest version or an unchanged one", () => {
    expect(historyChange(base, undefined)).toEqual([]);
    expect(historyChange({ ...base, version: 2 }, base)).toEqual([]);
  });
});
