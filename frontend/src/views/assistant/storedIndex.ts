// The index of the stored conversations (lib/assistantStorage.ts has the layout): read by
// the panel, and checked when a person signs in (another person's conversations go).
import {
  CONVERSATION_PREFIX,
  INDEX_KEY,
  bumpEpoch,
  type IndexEntry,
  type Owner,
  type StoredIndex,
} from "../../lib/assistantStorage";

function isOwner(v: unknown): v is Owner {
  if (!v || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o.subject === "string" &&
    typeof o.org === "string" &&
    typeof o.scope === "string" &&
    typeof o.user === "string"
  );
}

function isEntry(v: unknown): v is IndexEntry {
  if (!v || typeof v !== "object") return false;
  const e = v as Record<string, unknown>;
  return (
    typeof e.id === "string" &&
    isOwner(e.owner) &&
    typeof e.namespace === "string" &&
    typeof e.title === "string" &&
    typeof e.updated === "number" &&
    typeof e.bytes === "number"
  );
}

/** The stored index; an empty one when it is missing, unreadable or storage is refused. */
export function readIndex(): StoredIndex {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(INDEX_KEY) || "null");
    if (raw && typeof raw === "object") {
      const r = raw as Record<string, unknown>;
      const entries = Array.isArray(r.entries) ? r.entries.filter(isEntry) : [];
      const tombstones: Record<string, number> = {};
      if (r.tombstones && typeof r.tombstones === "object")
        for (const [id, at] of Object.entries(r.tombstones as Record<string, unknown>))
          if (typeof at === "number") tombstones[id] = at;
      return { entries, tombstones };
    }
  } catch {
    /* refused or garbage: nothing stored */
  }
  return { entries: [], tombstones: {} };
}

/** A different person or org signed in: delete what anyone else stored here. Answers
 *  whether anything went. */
export function forgetOtherPeople(subject: string, org: string): boolean {
  const index = readIndex();
  const others = index.entries.filter((e) => e.owner.subject !== subject || e.owner.org !== org);
  if (others.length === 0) return false;
  bumpEpoch();
  try {
    for (const e of others) localStorage.removeItem(CONVERSATION_PREFIX + e.id);
    const entries = index.entries.filter((e) => !others.includes(e));
    localStorage.setItem(INDEX_KEY, JSON.stringify({ entries, tombstones: {} }));
  } catch {
    /* refused */
  }
  return true;
}
