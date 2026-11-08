// Session briefs: the condensed, resumable state of a line of work that the
// coding plugins save at each compaction and at session end. The dashboard
// shows a conversation's brief next to the conversation it came from.
// Wire shapes mirror server/memory/api/models_session_brief.py.
import { post } from "../api";
import type { Identity } from "../api";

export interface BriefGetResponse {
  found: boolean;
  brief_key: string;
  revision: number;
  rendered: string;
  project: string;
  branch: string;
  head_sha: string;
  author: string;
  trigger: string;
  source_sessions: string[];
  updated_at: string | null;
  age_seconds: number;
  revisions: number;
  events_total: number;
  source_chars: number;
  brief_chars: number;
  deleted: boolean;
}

/** One brief: by key, or (key "" and `fromSession`) the newest brief the
 *  given coding session wrote. */
export async function getBrief(
  id: { org: string; namespace: string },
  briefKey: string,
  opts: { revision?: number; full?: boolean; fromSession?: string } = {},
): Promise<BriefGetResponse> {
  return post<BriefGetResponse>("/v1/session.brief_get", {
    org: id.org,
    namespace: id.namespace,
    agent: "loom-ui",
    brief_key: briefKey,
    from_session: briefKey ? "" : (opts.fromSession ?? ""),
    revision: opts.revision ?? 0,
    full: opts.full ?? false,
  });
}

export interface BriefSummary {
  brief_key: string;
  revision: number;
  project: string;
  branch: string;
  agent: string;
  goal: string;
  status: string;
  author: string;
  updated_at: string | null;
  age_seconds: number;
  brief_chars: number;
}

/** The namespace's briefs, most recently saved first. */
export async function listBriefs(
  id: { org: string; namespace: string },
  opts: { limit?: number } = {},
): Promise<BriefSummary[]> {
  const out = await post<{ briefs: BriefSummary[] }>("/v1/session.brief_list", {
    org: id.org,
    namespace: id.namespace,
    agent: "loom-ui",
    limit: opts.limit ?? 20,
  });
  return out.briefs || [];
}

/** POST /v1/session.brief_delete: the brief is forgotten (a tombstoned revision). */
export async function deleteBrief(
  id: Pick<Identity, "org" | "namespace">,
  briefKey: string,
): Promise<{ deleted: boolean }> {
  return post("/v1/session.brief_delete", {
    org: id.org,
    namespace: id.namespace,
    agent: "loom-ui",
    brief_key: briefKey,
  });
}
