// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

// ===== Conversations =====

export type ChatRole = "user" | "assistant" | "system" | "tool";

export interface ConversationHit {
  thread_id: string;
  msg_id: string;
  role: ChatRole;
  content_excerpt: string;
  ts: string;
  namespace?: string; // populated on cross-namespace search
}

export type SearchScope = "this" | "all_mine";

export async function searchConversations(
  id: Identity,
  query: string,
  opts: {
    thread_id?: string;
    role?: ChatRole;
    top_k?: number;
    namespace_scope?: SearchScope;
    agent?: string;
  } = {},
): Promise<{ hits: ConversationHit[] }> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    // Operator browse spans every agent's threads by default ("*"); an
    // explicit agent narrows to one.
    agent: opts.agent ?? "*",
    query,
    top_k: opts.top_k || 50,
    // Server default is "all_mine" (cross-namespace fan-out).
    ...(opts.namespace_scope ? { namespace_scope: opts.namespace_scope } : {}),
  };
  if (opts.thread_id) body["thread_id"] = opts.thread_id;
  if (opts.role) body["role"] = opts.role;
  return post<{ hits: ConversationHit[] }>("/v1/conversation.search", body);
}

export interface ConversationThreadSummary {
  thread_id: string;
  agent: string;
  msg_count: number;
  first_seen: string;
  last_seen: string;
  last_role: string;
  last_content_excerpt: string;
  title?: string;
}

export interface ConversationMessage {
  msg_id: string;
  /** The turn's writer; a thread read with agent "*" holds several. */
  agent: string;
  role: ChatRole;
  content: string;
  ts: string;
  reasoning?: string;
  tokens?: number;
  run_id?: string;
  user_id?: string;
  metadata?: Record<string, unknown>;
}

export interface ConversationThreadDetail {
  found: boolean;
  thread_id: string;
  title: string;
  agent: string;
  msg_count: number;
  created_at: string | null;
  updated_at: string | null;
  messages: ConversationMessage[];
  /** The conversation.list cursor for the turns after these; absent at the end. */
  next_cursor?: string | null;
}

/** Fetch one thread with its full message list, for the Conversations
 *  preview dialog. The server caps at 1000 messages per call. */
export async function getConversationThread(
  id: Pick<Identity, "org" | "namespace">,
  agent: string,
  thread_id: string,
  opts: { limit?: number } & RequestOpts = {},
): Promise<ConversationThreadDetail> {
  return post(
    "/v1/conversation.get",
    {
      org: id.org,
      namespace: id.namespace,
      agent,
      thread_id,
      include_messages: true,
      limit: opts.limit ?? 200,
    },
    opts.signal,
  );
}

/** Cursor-paginated walk of one thread's messages. Pairs with
 *  conversation.get (which returns the first window); drives the
 *  Inspector dialog's "Load more" affordance on long threads. */
export async function listConversationMessages(
  id: Pick<Identity, "org" | "namespace">,
  agent: string,
  thread_id: string,
  opts: { cursor?: string | null; limit?: number } & RequestOpts = {},
): Promise<{ rows: ConversationMessage[]; next_cursor: string | null }> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    agent,
    thread_id,
    limit: opts.limit ?? 100,
  };
  if (opts.cursor) body.cursor = opts.cursor;
  return post("/v1/conversation.list", body, opts.signal);
}

// The largest `offset` conversation.threads serves (its `le`); a larger one is a 422.
export const THREADS_MAX_OFFSET = 100_000;

/** Recent threads on a namespace. Drives the Sessions view's default
 *  landing surface so users with no query have somewhere to click. */
export async function listConversationThreads(
  id: Pick<Identity, "org" | "namespace">,
  opts: { agent?: string; window_days?: number; limit?: number; offset?: number; fields?: string[] } & RequestOpts = {},
): Promise<{ threads: ConversationThreadSummary[]; ignored_args?: string[] }> {
  // A server a release behind ignores `offset` and names it in ignored_args.
  return post(
    "/v1/conversation.threads",
    {
      org: id.org,
      namespace: id.namespace,
      agent: opts.agent ?? "*",
      window_days: opts.window_days ?? 30,
      limit: opts.limit ?? 20,
      ...(opts.offset ? { offset: opts.offset } : {}),
      ...(opts.fields ? { fields: opts.fields } : {}),
    },
    opts.signal,
  );
}

// ===== Learnings =====

export type LearningStatus = "pending" | "promoted" | "rejected";
export type ReviewDecision = "promote" | "reject";

export interface LearningEntry {
  candidate_id: string;
  memory_key: string;
  memory_type: string;
  proposed_value: unknown;
  // Supporters behind the candidate; for `assistant_turn`, the distinct end users.
  supporter_count: number;
  // `assistant_turn` rows: distinct sessions that stated the rule. Absent from a
  // server a release behind.
  supporter_sessions?: number;
  // `assistant_turn` rows: distinct principals that wrote the supporter rows; one
  // token can send many user_ids. Absent from a server a release behind.
  supporter_writers?: number;
  confidence_avg: number;
  supporters: string[];
  rationale: string;
  // Which producer filed it; `assistant_turn` is a rule a bot stated about itself.
  source_kind?: string;
  // The agent an `assistant_turn` rule is for; "" otherwise.
  agent?: string;
  status: LearningStatus;
  proposed_at: string;
  decided_at: string | null;
  decided_by: string;
  decision_reason: string;
}

export async function listLearnings(
  id: Pick<Identity, "org" | "namespace">,
  status: LearningStatus = "pending",
  limit: number = 50,
  opts: RequestOpts = {},
): Promise<{ rows: LearningEntry[] }> {
  return post<{ rows: LearningEntry[] }>(
    "/v1/learnings.list",
    {
      org: id.org,
      namespace: id.namespace,
      status,
      limit,
    },
    opts.signal,
  );
}

export async function reviewLearning(
  id: Pick<Identity, "org" | "namespace">,
  candidate_id: string,
  decision: ReviewDecision,
  reason: string = "",
): Promise<{
  candidate_id: string;
  new_status: LearningStatus;
  promoted_to_shared: boolean;
  decided_at: string;
}> {
  return post("/v1/learnings.review", {
    org: id.org,
    namespace: id.namespace,
    candidate_id,
    decision,
    reason,
  });
}

/** POST /v1/conversation.update: a thread's new title. */
export async function renameThread(
  id: Pick<Identity, "org" | "namespace">,
  agent: string,
  threadId: string,
  title: string,
): Promise<{ updated: boolean }> {
  return post("/v1/conversation.update", { org: id.org, namespace: id.namespace, agent, thread_id: threadId, title });
}

/** POST /v1/conversation.delete (ADMIN): tombstones the thread and its messages; the
 *  server asks for the thread id again as its confirmation. */
export async function deleteThread(
  id: Pick<Identity, "org" | "namespace">,
  agent: string,
  threadId: string,
): Promise<{ total: number }> {
  return post("/v1/conversation.delete", {
    org: id.org,
    namespace: id.namespace,
    agent,
    thread_id: threadId,
    confirm_thread_id: threadId,
  });
}
