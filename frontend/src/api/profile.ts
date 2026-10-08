// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";
import type { Identity, MemoryListResponse, RequestOpts } from "../api";

// ----------------------------------------------------------------------
// Profile — always-loaded prompt context the user (or their agents) edit.
// Stored as memory rows with memory_type='profile' + a server-enforced
// char_limit. Reads route through memory.list / memory.get because
// memory.list omits the value column.
// ----------------------------------------------------------------------

export interface ProfileEntry {
  label: string;
  value: string;
  char_limit: number | null;
  /** Server-canonical SHA-256 of the JSON-serialized value. Used as
   *  if_match_sha256 on edits to detect concurrent writes. */
  content_sha256: string;
  version: number;
  created_at: string;
  updated_at?: string | null;
  /** Who last wrote this section. Lets the UI distinguish agent
   *  self-edits (via loom_profile_* MCP tools) from human edits
   *  (agent='loom-ui'). */
  last_edited_by?: string;
}

export interface ProfileListResponse {
  entries: ProfileEntry[];
  truncated: boolean;
}

/** List all profile sections under the identity. Sections are memory
 *  rows with memory_type='profile'; fetches the bare list then reads
 *  each value via memory.get since memory.list omits the value column.
 *
 *  Uses `agent='*'` so the page shows sections written by any agent in
 *  the namespace (human edits land as agent='loom-ui', agent self-edits
 *  land as the agent's own identifier). Without the wildcard the page
 *  only shows sections the currently-selected agent wrote. */
export async function listProfile(id: Identity, opts: RequestOpts = {}): Promise<ProfileListResponse> {
  // The list carries each section's value (the stored preview), so the page reads the row
  // only when the list says the value was cut — no memory.get per section in series on
  // the paint path.
  const listed = await post<MemoryListResponse>(
    "/v1/memory.list",
    {
      ...id,
      agent: "*",
      memory_type: "profile",
      limit: 200,
    },
    opts.signal,
  );
  const entries: ProfileEntry[] = await Promise.all(
    (listed.entries || []).map(async (m) => {
      const fromList = {
        label: m.memory_key,
        value: typeof m.value === "string" ? m.value : JSON.stringify(m.value ?? ""),
        char_limit: m.char_limit ?? null,
        content_sha256: m.content_sha256 || "",
        version: Number(m.version ?? 0),
        created_at: m.created_at,
        updated_at: m.updated_at,
        last_edited_by: m.agent || undefined,
      };
      if (!m.value_truncated && m.value !== undefined) return fromList;
      // The row holds more than the preview: read it under the agent that
      // wrote it (memory.get is keyed on the writer's identity tuple).
      const getId = { ...id, agent: m.agent || id.agent };
      const got = await post<{
        found: boolean;
        value?: unknown;
        char_limit?: number | null;
        content_sha256?: string;
        version?: number;
        created_at?: string;
        updated_at?: string | null;
      }>("/v1/memory.get", { ...getId, memory_key: m.memory_key }, opts.signal);
      return {
        ...fromList,
        value: typeof got.value === "string" ? got.value : JSON.stringify(got.value ?? ""),
        char_limit: got.char_limit ?? fromList.char_limit,
        content_sha256: got.content_sha256 || fromList.content_sha256,
        version: Number(got.version ?? fromList.version),
        created_at: got.created_at ?? fromList.created_at,
        updated_at: got.updated_at ?? fromList.updated_at,
      };
    }),
  );
  return { entries, truncated: listed.truncated };
}

/** Create or replace a profile section. Server enforces char_limit on
 *  the serialized value; 422 on overflow. */
export async function setProfileSection(
  id: Identity,
  label: string,
  value: string,
  charLimit: number,
): Promise<{ version: string; content_sha256: string; value_length: number }> {
  return post<{ version: string; content_sha256: string; value_length: number }>("/v1/profile.set", {
    ...id,
    label,
    value,
    char_limit: charLimit,
  });
}

/** Tombstone a profile section (14-day grace, restorable). Sections
 *  live in loom.memory so memory.delete is the right primitive. */
export async function deleteProfileSection(id: Identity, label: string): Promise<void> {
  await post<void>("/v1/memory.delete", { ...id, memory_key: label });
}

export interface ProfileActivityEntry {
  agent: string;
  /** ISO-8601 UTC of the most recent profile-carrying call from this agent
   * against the namespace (loom_activate, profile.render, context.pack, mcp.initialize). */
  last_activated_at: string;
  event_count: number;
  /** The read paths this agent used, sorted. */
  operations: string[];
}

/** Which MCP clients have read the namespace's profile recently?
 *  Answers the /me/profile trust question "is my agent actually picking
 *  this up?". Reads from loom.agent_events where operation IN
 *  ('loom_activate', 'profile.render'), one row per agent ordered by
 *  recency. Default lookback 7 days; 30 max server-side. */
export async function getProfileActivity(id: Identity, lookbackDays = 7): Promise<{ entries: ProfileActivityEntry[] }> {
  return post<{ entries: ProfileActivityEntry[] }>("/v1/profile.activity", {
    ...id,
    agent: "*",
    lookback_days: lookbackDays,
  });
}
