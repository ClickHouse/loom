// The automatic retirements' wording and the restores read beside them, for the Review
// queue and the Audit log. Kept out of audit.ts, which the sidebar badge pulls into the index
// chunk.
import type { Identity, RequestOpts } from "../api";
import { parseChTime } from "../lib/format";
import { listAuditLog, parseAuditSubject, type AutoResolveReason } from "./audit";

// ===== automatic retirements: wording =====

/** Why Loom retired a memory, in plain words. No pronoun, so it reads for one row in the
 *  Review queue and for "Loom retired N memories automatically (…)" in the Audit log. */
export const AUTO_RESOLVE_REASON_TEXT: Record<AutoResolveReason, string> = {
  conflict_resolver: "Replaced by a later statement of the same attribute",
  same_key_newest_wins: "Replaced by a newer write (namespace policy)",
  newest_wins_contradiction: "Replaced by a newer write (namespace policy)",
  value_update: "Replaced by an updated value",
};

/** The reason's wording; null for an unknown reason. */
export function autoResolveReasonText(reason: string): string | null {
  return Object.hasOwn(AUTO_RESOLVE_REASON_TEXT, reason) ? AUTO_RESOLVE_REASON_TEXT[reason as AutoResolveReason] : null;
}

/** An audit row's time in ms: one without an offset is UTC (`parseChTime`); 0 if unreadable. */
export const auditInstant = (ts: string): number => parseChTime(ts)?.getTime() ?? 0;

/** Pages of `memory.unsupersede` records `listUnsuperseded` reads at most. A restore is
 *  one record, made by a person clicking Undo or Restore, so 5 pages of 500 cover far more
 *  restores than a 30-day window holds; past that, an older retirement shows Undo: undoing
 *  a row an Undo restored answers restored, one changed since `restored=false`
 *  ("Changed since"). */
export const UNSUPERSEDED_MAX_PAGES = 5;

/** memory_id -> when it was last restored from `superseded` (`memory.unsupersede` audit
 *  records since `since`, the oldest retirement on show), so a retirement already undone
 *  reads as such. Follows `next_cursor` for at most `UNSUPERSEDED_MAX_PAGES` pages. */
export async function listUnsuperseded(
  id: Pick<Identity, "org" | "namespace">,
  opts: { since: string; limit?: number; maxPages?: number } & RequestOpts,
): Promise<Map<string, string>> {
  const out = new Map<string, string>();
  let cursor: string | undefined;
  for (let page = 0; page < (opts.maxPages ?? UNSUPERSEDED_MAX_PAGES); page++) {
    const { rows, next_cursor } = await listAuditLog(id, {
      event_type: "memory.unsupersede",
      limit: opts.limit ?? 500,
      since: opts.since,
      cursor,
      fields: ["ts", "subject"],
      signal: opts.signal,
    });
    for (const r of rows || []) {
      const mid = String(parseAuditSubject(r.subject)["memory_id"] ?? "");
      if (!mid) continue;
      const prev = out.get(mid);
      // As instants (a whole second comes without a fraction, and "." sorts before "Z"),
      // and a time without an offset as UTC, as ClickHouse sends it.
      if (!prev || auditInstant(r.ts) > auditInstant(prev)) out.set(mid, r.ts);
    }
    if (!next_cursor) break;
    cursor = next_cursor;
  }
  return out;
}
