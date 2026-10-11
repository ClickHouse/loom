import { describe, expect, it } from "vitest";
import type { MemoryHistoryEntry } from "../api/admin";
import { historyChange, previousVersion } from "./historyChange";

const base: MemoryHistoryEntry = {
  memory_id: "m-a",
  version: 1,
  value: "same text",
  content_sha256: "s1",
  created_at: "2026-01-01T00:00:00Z",
  tombstoned_at: null,
  redacted_at: null,
  source_trace: "",
  confidence: 0.75,
  status: "active",
  conflict_with: [],
  tags: ["kind:observation"],
  verified_at: null,
  verified_by: "",
  lock_mode: "",
  lock_reason: "",
  lock_until: null,
};
const next = (patch: Partial<MemoryHistoryEntry>): MemoryHistoryEntry => ({ ...base, version: 2, ...patch });

describe("historyChange", () => {
  it("names a conflict mark with the value unchanged", () => {
    expect(historyChange(next({ status: "conflicted", conflict_with: ["a", "b", "c"] }), base)).toEqual([
      "marked conflicted · 3 memories",
    ]);
  });

  it("names conflict links that change while the status stays conflicted", () => {
    const one = next({ status: "conflicted", conflict_with: ["a"] });
    expect(historyChange({ ...one, version: 3, conflict_with: ["a", "b"] }, one)).toEqual(["conflicts +1 memory"]);
    expect(historyChange({ ...one, version: 3, conflict_with: ["b"] }, one)).toEqual(["conflicts +1 memory −1 memory"]);
  });

  it("names tag, verification, lock and pin changes", () => {
    const h = next({
      tags: ["kind:observation", "owner:new"],
      verified_at: "2026-01-02T00:00:00Z",
      verified_by: "human:r1",
      lock_mode: "frozen",
      pinned: true,
    });
    expect(historyChange(h, base)).toEqual(["tags +owner:new", "verified by human:r1", "locked frozen", "pinned"]);
    expect(historyChange(base, h)).toEqual(["tags −owner:new", "verification revoked", "unlocked", "unpinned"]);
  });

  it("names a second verifier within the same second", () => {
    const v1 = next({ verified_at: "2026-01-02T00:00:00Z", verified_by: "human:reviewer1" });
    expect(historyChange({ ...v1, version: 3, verified_by: "human:reviewer2" }, v1)).toEqual([
      "re-verified by human:reviewer2",
    ]);
  });

  it("names a lock update that keeps its mode", () => {
    const finite = next({ lock_mode: "no_delete", lock_until: "2026-01-02T00:10:00Z", lock_reason: "audit" });
    expect(historyChange({ ...finite, version: 3, lock_until: null, lock_reason: "hold" }, finite)).toEqual([
      "lock extended indefinitely",
    ]);
    expect(historyChange({ ...finite, version: 3, lock_until: "2026-01-03T00:00:00Z" }, finite)).toEqual([
      "lock extended",
    ]);
    expect(historyChange({ ...finite, version: 3, lock_reason: "hold" }, finite)).toEqual(["lock updated"]);
  });

  it("says a same-value version changed metadata when no field it reads differs", () => {
    expect(historyChange(next({}), base)).toEqual(["metadata updated"]);
    expect(historyChange(next({ value: "new", content_sha256: "s2" }), base)).toEqual([]);
    expect(historyChange(base, undefined)).toEqual([]);
  });
});

describe("previousVersion", () => {
  it("skips other writers' rows of the key", () => {
    const rows = [
      next({ version: 3, tags: ["owner:new"] }),
      next({ memory_id: "m-b", version: 2, verified_at: "2026-01-02T00:00:00Z", lock_mode: "frozen" }),
      { ...base, tags: ["owner:old"] },
    ];
    expect(previousVersion(rows, 0)).toBe(rows[2]);
    expect(previousVersion(rows, 1)).toBeUndefined();
  });
});
