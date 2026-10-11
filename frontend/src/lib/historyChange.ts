import type { MemoryHistoryEntry } from "../api/admin";

/** The version before `history[idx]` of the same memory: a namespace-wide history
 * interleaves several writers' rows of the key, newest first. */
export function previousVersion(history: MemoryHistoryEntry[], idx: number): MemoryHistoryEntry | undefined {
  const id = history[idx]?.memory_id;
  return history.slice(idx + 1).find((o) => o.memory_id === id);
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function setDiff(now: readonly string[] | undefined, before: readonly string[] | undefined) {
  const a = new Set(now ?? []);
  const b = new Set(before ?? []);
  return { added: [...a].filter((x) => !b.has(x)), removed: [...b].filter((x) => !a.has(x)) };
}

/** What a version changed besides its value, against the version before it:
 * the reason a version that reads like its predecessor is in the history. */
export function historyChange(h: MemoryHistoryEntry, prev: MemoryHistoryEntry | undefined): string[] {
  if (!prev) return [];
  const out: string[] = [];

  const status = h.status ?? "active";
  const conflicts = setDiff(h.conflict_with, prev.conflict_with);
  if (status !== (prev.status ?? "active")) {
    const n = h.conflict_with?.length ?? 0;
    out.push(
      status === "conflicted" && n ? `marked conflicted · ${plural(n, "memory", "memories")}` : `status ${status}`,
    );
  } else if (conflicts.added.length || conflicts.removed.length) {
    const parts = [];
    if (conflicts.added.length) parts.push(`+${plural(conflicts.added.length, "memory", "memories")}`);
    if (conflicts.removed.length) parts.push(`−${plural(conflicts.removed.length, "memory", "memories")}`);
    out.push(`conflicts ${parts.join(" ")}`);
  }

  const tags = setDiff(h.tags, prev.tags);
  if (tags.added.length || tags.removed.length) {
    out.push(["tags", ...tags.added.map((t) => `+${t}`), ...tags.removed.map((t) => `−${t}`)].join(" "));
  }

  const by = h.verified_by ? ` by ${h.verified_by}` : "";
  if (!!h.verified_at !== !!prev.verified_at) {
    out.push(h.verified_at ? `verified${by}` : "verification revoked");
  } else if (
    h.verified_at &&
    (h.verified_by !== prev.verified_by ||
      h.verified_at !== prev.verified_at ||
      h.verified_sha256 !== prev.verified_sha256)
  ) {
    out.push(`re-verified${by}`);
  }

  const mode = h.lock_mode || "";
  if (mode !== (prev.lock_mode || "")) {
    out.push(mode ? `locked ${mode}` : "unlocked");
  } else if (mode && (h.lock_until ?? null) !== (prev.lock_until ?? null)) {
    const longer = !h.lock_until || (!!prev.lock_until && h.lock_until > prev.lock_until);
    out.push(longer ? (h.lock_until ? "lock extended" : "lock extended indefinitely") : "lock shortened");
  } else if (mode && (h.lock_reason || "") !== (prev.lock_reason || "")) {
    out.push("lock updated");
  }

  if (!!h.pinned !== !!prev.pinned) out.push(h.pinned ? "pinned" : "unpinned");
  if (h.confidence !== undefined && prev.confidence !== undefined && h.confidence !== prev.confidence) {
    out.push(`confidence ${prev.confidence} → ${h.confidence}`);
  }
  if (prev.tombstoned_at && !h.tombstoned_at) out.push("restored");

  // A stored change none of the above reads: say so, not nothing.
  if (!out.length && h.content_sha256 && h.content_sha256 === prev.content_sha256) out.push("metadata updated");
  return out;
}
