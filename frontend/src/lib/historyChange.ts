import type { MemoryHistoryEntry } from "../api/admin";

/** What a version changed besides its value, against the version before it:
 * the reason a version that reads like its predecessor is in the history. */
export function historyChange(h: MemoryHistoryEntry, prev: MemoryHistoryEntry | undefined): string[] {
  if (!prev) return [];
  const out: string[] = [];
  const status = h.status ?? "active";
  if (status !== (prev.status ?? "active")) {
    const n = h.conflict_with?.length ?? 0;
    out.push(
      status === "conflicted" && n ? `marked conflicted · ${n} ${n === 1 ? "memory" : "memories"}` : `status ${status}`,
    );
  }
  const tags = new Set(h.tags ?? []);
  const prevTags = new Set(prev.tags ?? []);
  const added = [...tags].filter((t) => !prevTags.has(t));
  const removed = [...prevTags].filter((t) => !tags.has(t));
  if (added.length || removed.length) {
    out.push(["tags", ...added.map((t) => `+${t}`), ...removed.map((t) => `−${t}`)].join(" "));
  }
  if (!!h.verified_at !== !!prev.verified_at) out.push(h.verified_at ? "verified" : "verification revoked");
  if ((h.lock_mode || "") !== (prev.lock_mode || "")) out.push(h.lock_mode ? `locked ${h.lock_mode}` : "unlocked");
  if (!!h.pinned !== !!prev.pinned) out.push(h.pinned ? "pinned" : "unpinned");
  if (h.confidence !== undefined && prev.confidence !== undefined && h.confidence !== prev.confidence) {
    out.push(`confidence ${prev.confidence} → ${h.confidence}`);
  }
  if (!!h.tombstoned_at !== !!prev.tombstoned_at && !h.tombstoned_at) out.push("restored");
  return out;
}
