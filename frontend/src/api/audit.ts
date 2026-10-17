// The audit-log read and the automatic retirements parsed out of it. Its own module so the
// sidebar's Review queue badge can import it statically: a module rides in one chunk, and
// importing anything from admin.ts would pull all of it into the index.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

// ===== audit log =====

export interface AuditEntry {
  ts: string;
  event_type: string;
  actor: string;
  /** JSON text of the operation's input (`AuditListRow.subject`); parse before reading. */
  subject: string;
  request_id: string;
}

export async function listAuditLog(
  id: Pick<Identity, "org"> & { namespace?: string },
  opts: { limit?: number; event_type?: string; since?: string; fields?: string[]; cursor?: string } & RequestOpts = {},
): Promise<{ rows: AuditEntry[]; next_cursor?: string | null }> {
  const body: Record<string, unknown> = {
    org: id.org,
    limit: opts.limit ?? 100,
    // The fields the caller reads; unset, every field.
    ...(opts.fields ? { fields: opts.fields } : {}),
  };
  // A time bound. The table is sorted by event type before time, so an unbounded "newest
  // 100" reads the scope's whole recent history (~9 MB for a 27 kB page).
  if (opts.since) body["since"] = opts.since;
  // namespace omitted = org-wide (handler scopes platform admins to
  // all, regular users to their accessible namespaces).
  if (id.namespace) body["namespace"] = id.namespace;
  if (opts.event_type) body["event_type"] = opts.event_type;
  // The previous page's `next_cursor`: the next, older page.
  if (opts.cursor) body["cursor"] = opts.cursor;
  return post("/v1/audit.list", body, opts.signal);
}

// ===== automatic retirements =====

/** Why Loom retired a memory on its own (`server/memory/app/auto_resolution.py`). */
export type AutoResolveReason =
  | "conflict_resolver"
  | "same_key_newest_wins"
  | "newest_wins_contradiction"
  | "value_update";

/** One retired row of a `memory.auto_supersede` audit row: ids, keys and identity, no value. */
export interface AutoResolvedRow {
  memory_id: string;
  memory_key: string;
  agent: string;
  user_id: string;
  session_id: string;
  superseded_by: string;
  superseded_by_key?: string;
  /** The judge's label (`contradiction` / `elaboration`) on a same-key judgement. */
  label?: string;
  /** The re-read after the write could not confirm the retirement took effect. */
  unconfirmed?: boolean;
  reason: AutoResolveReason | string;
  /** When the retirement was recorded (the audit row's `ts`). */
  ts: string;
}

/** How far back the Auto-resolved tab looks by default. */
export const AUTO_RESOLVED_WINDOW_MS = 30 * 24 * 60 * 60 * 1000;

/** An audit row's `subject` JSON as an object; {} when it is not one. */
export function parseAuditSubject(raw: string): Record<string, unknown> {
  try {
    const v: unknown = JSON.parse(raw);
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

const str = (v: unknown): string => (v == null ? "" : String(v));

/** One page of the memories Loom retired on its own in a namespace, newest first: one row
 *  per retired memory, flattened out of the `memory.auto_supersede` audit records. A page
 *  is `limit` records (default 20), each listing up to 200 retired memories. `since`
 *  defaults to 30 days back; pass the returned `next_cursor` as `cursor` for the next,
 *  older page. */
export async function listAutoResolved(
  id: Pick<Identity, "org" | "namespace">,
  opts: { limit?: number; since?: string; cursor?: string } & RequestOpts = {},
): Promise<{ rows: AutoResolvedRow[]; next_cursor: string | null }> {
  const page = await listAuditLog(id, {
    event_type: "memory.auto_supersede",
    limit: opts.limit ?? 20,
    since: opts.since ?? new Date(Date.now() - AUTO_RESOLVED_WINDOW_MS).toISOString(),
    cursor: opts.cursor,
    fields: ["ts", "subject"],
    signal: opts.signal,
  });
  const out: AutoResolvedRow[] = [];
  for (const r of page.rows || []) {
    const subject = parseAuditSubject(r.subject);
    const retired = Array.isArray(subject["retired"]) ? (subject["retired"] as unknown[]) : [];
    for (const e of retired) {
      if (!e || typeof e !== "object" || Array.isArray(e)) continue;
      const m = e as Record<string, unknown>;
      if (!m["memory_id"] || !m["memory_key"]) continue;
      out.push({
        memory_id: str(m["memory_id"]),
        memory_key: str(m["memory_key"]),
        agent: str(m["agent"]) || "-",
        user_id: str(m["user_id"]) || "-",
        session_id: str(m["session_id"]) || "-",
        superseded_by: str(m["superseded_by"]),
        superseded_by_key: m["superseded_by_key"] ? str(m["superseded_by_key"]) : undefined,
        label: m["label"] ? str(m["label"]) : undefined,
        unconfirmed: m["unconfirmed"] === "true" || undefined,
        reason: str(subject["reason"]),
        ts: r.ts,
      });
    }
  }
  return { rows: out, next_cursor: page.next_cursor ?? null };
}

/** Whether the Auto-resolved tab has a row to show: its newest record, read with the
 *  tab's filter. The sidebar shows the Review queue for it when there is no open conflict. */
export async function hasAutoResolved(
  id: Pick<Identity, "org" | "namespace">,
  opts: RequestOpts = {},
): Promise<boolean> {
  const page = await listAutoResolved(id, { limit: 1, signal: opts.signal });
  return page.rows.length > 0;
}
