/**
 * `Agent` and `Datapack` — the identity-bound entry points, mirroring the
 * Python SDK.
 *
 * Loom addresses every memory by the identity tuple
 * (org, namespace, agent, user_id, session_id, run_id). An `Agent` holds the
 * first three and fills them into every call; `user_id` / `session_id` /
 * `run_id` are per-call and default to the wire's sentinel `"-"` when the op
 * has the field. A `Datapack` holds (org, namespace) plus the platform
 * defaults so `.agent(name)` spawns Agents without repeating them.
 *
 * The `consumer` option names the product this process
 * speaks for — the ledger's `consumer` column, what the
 * recall credit keys on. The transport stamps it on every
 * body that names none; `agent.consumer` reads it back.
 *
 *     const pack = new Datapack("acme", "support", { baseUrl, apiKey });
 *     const bot = pack.agent("assistant");
 *     await bot.remember("user_seat_pref", "Prefers an aisle seat", { userId: "u1" });
 *     const hits = await bot.recall("seat preference", { userId: "u1" });
 *     const answer = await bot.ask("which seat should I book?", { userId: "u1" });
 */

import type { AccountClient } from "./account.js";
import { LoomClient, type LoomClientOptions, type OpInput, type OpOutput } from "./client.js";
import type { OpName } from "./generated/ops.js";
import { envVar, scriptName } from "./runtime.js";
import { Session, type SessionOptions } from "./session.js";
import { AgentTools, type AgentToolsOptions } from "./tools.js";
import type { CallOptions } from "./transport.js";

export const SENTINEL = "-";

export interface Scope {
  userId?: string;
  sessionId?: string;
  runId?: string;
  /** Override the constructor-bound agent for this call only (orchestrators). */
  agentId?: string;
}

type IdentityFields = {
  org: string;
  namespace: string;
  agent?: string;
  user_id?: string | null;
  session_id?: string | null;
  run_id?: string | null;
};

/** Everything of an op's input except the identity the Agent fills in. */
export type Body<K extends OpName> = Omit<OpInput<K>, keyof IdentityFields> &
  Partial<Pick<OpInput<K>, Extract<keyof OpInput<K>, "agent">>>;

export class Datapack {
  readonly client: LoomClient;
  constructor(
    readonly org: string,
    readonly namespace: string,
    opts: LoomClientOptions | LoomClient,
  ) {
    this.client = opts instanceof LoomClient ? opts : new LoomClient(opts);
  }
  agent(name: string): Agent {
    return new Agent(this.org, this.namespace, name, this.client);
  }
}

/** An agent that names itself `""`, `"-"` or `"*"` is every
 *  other unnamed caller to the ledger, the mentor and the cost views. The
 *  default is the process's script name under an `sdk-ts:` prefix. */
export function defaultAgentName(agent: string | undefined): string {
  const given = (agent ?? "").trim();
  if (given && given !== "-" && given !== "*") return given;
  return `sdk-ts:${scriptName() ?? "bot"}`;
}

/** A verb's options, split into the identity scope and the op's own fields. */
function splitScope<T extends Scope>(scope: T): [Scope, Omit<T, keyof Scope>] {
  const { userId, sessionId, runId, agentId, ...rest } = scope;
  return [{ userId, sessionId, runId, agentId }, rest];
}

/** Reads span every agent unless the caller names one, as in the Python SDK and the server default. */
function readScope(ids: Scope): Scope {
  return { ...ids, agentId: ids.agentId ?? "*" };
}

export class Agent {
  readonly client: LoomClient;
  readonly agent: string;
  private mentorBriefStarted = false;
  constructor(
    readonly org: string,
    readonly namespace: string,
    agent: string,
    opts: LoomClientOptions | LoomClient,
  ) {
    this.client = opts instanceof LoomClient ? opts : new LoomClient(opts);
    this.agent = defaultAgentName(agent);
    if (this.agent !== agent) {
      this.client.transport.log.warn(
        `loom: agent name "${agent}" is anonymous; using "${this.agent}" — pass a real agent name so the mentor, cost and provenance can tell this bot apart`,
      );
    }
  }

  /**
   * The store's advice to this agent — `mentor.brief` for
   * (org, namespace, agent): the planes it never uses, the hints with the
   * counts behind them, and whether past hints were followed. Logs up to
   * three hints at INFO and returns the brief. Call it once at startup; a
   * bot that never renders a context pack has no other session-start
   * channel. Per-call hints still arrive on every response (`X-Loom-Hint`,
   * logged by the transport) without this call.
   */
  async mentor(opts: { windowSeconds?: number } = {}): Promise<OpOutput<"mentor.brief">> {
    const brief = await this.client.call("mentor.brief", {
      org: this.org,
      namespace: this.namespace,
      agent: this.agent,
      ...(opts.windowSeconds ? { window_seconds: opts.windowSeconds } : {}),
    });
    for (const h of (brief.hints ?? []).slice(0, 3)) {
      if (h.text) this.client.transport.log.info(`loom mentor (${h.code}, ${h.severity}): ${h.text}`);
    }
    return brief;
  }

  /**
   * How to use Loom, as Markdown — `mentor.guide`: the loop, where each kind
   * of state goes and how to read answers (`"skill"`), every plane with its
   * ops (`"reference"`), or both (`"all"`). Put it in the system prompt of an
   * agent that has no plugin to carry it.
   */
  async guide(part: "skill" | "reference" | "all" = "skill"): Promise<string> {
    const out = await this.client.call("mentor.guide", { part });
    return out.text ?? "";
  }

  /** With `LOOM_MENTOR_BRIEF=on`, the first call of an Agent also runs
   *  `mentor()` in the background (opt-in: a hidden request surprises a test
   *  that counts them; the explicit `mentor()` is the default path). */
  private startMentorBrief(): void {
    if (this.mentorBriefStarted) return;
    this.mentorBriefStarted = true;
    if ((envVar("LOOM_MENTOR_BRIEF") ?? "").toLowerCase() !== "on") return;
    void this.mentor().catch(() => undefined);
  }

  /** Your own tokens, through this Agent's client: see `AccountClient`. */
  get account(): AccountClient {
    return this.client.account;
  }

  /** The consumer label the transport stamps on every body; `""` = none. */
  get consumer(): string {
    return this.client.transport.consumer;
  }

  /** The identity fields this Agent stamps on a call, for the given per-call scope. */
  identity(scope: Scope = {}): IdentityFields {
    return {
      org: this.org,
      namespace: this.namespace,
      agent: scope.agentId ?? this.agent,
      user_id: scope.userId ?? SENTINEL,
      session_id: scope.sessionId ?? SENTINEL,
      run_id: scope.runId ?? SENTINEL,
    };
  }

  /**
   * Call any op with the identity filled in. Fields the op's input does not
   * declare are dropped by the server's model, so stamping all six is safe
   * for every op; the ones you pass in `body` win.
   */
  call<K extends OpName>(name: K, body: Body<K>, scope: Scope = {}, opts?: CallOptions): Promise<OpOutput<K>> {
    if (name !== "mentor.brief") this.startMentorBrief();
    const input = { ...this.identity(scope), ...(body as object) } as OpInput<K>;
    return this.client.call(name, input, opts);
  }

  // ---- the memory verbs an agent loop uses -----------------------------------

  /** Write or update one memory by key (`memory.set`). */
  remember(memoryKey: string, value: string, scope: Scope & Omit<Body<"memory.set">, "memory_key" | "value"> = {}) {
    const [ids, rest] = splitScope(scope);
    return this.call("memory.set", { memory_key: memoryKey, value, ...rest } as Body<"memory.set">, ids);
  }

  /** Semantic search over every agent's memories in the namespace (`memory.search`); `agentId` narrows. */
  recall(query: string, scope: Scope & Omit<Body<"memory.search">, "query"> = {}) {
    const [ids, rest] = splitScope(scope);
    return this.call("memory.search", { query, ...rest } as Body<"memory.search">, readScope(ids));
  }

  /** Grounded answer with citations, or an abstention (`memory.ask`); reads like `recall`. */
  ask(query: string, scope: Scope & Omit<Body<"memory.ask">, "query"> = {}) {
    const [ids, rest] = splitScope(scope);
    return this.call("memory.ask", { query, ...rest } as Body<"memory.ask">, readScope(ids));
  }

  /** Extract memories from a conversation's turns (`memory.set_from_messages`). */
  ingest(
    messages: OpInput<"memory.set_from_messages">["messages"],
    scope: Scope & Omit<Body<"memory.set_from_messages">, "messages"> = {},
  ) {
    const [ids, rest] = splitScope(scope);
    return this.call("memory.set_from_messages", { messages, ...rest } as Body<"memory.set_from_messages">, ids);
  }

  /** Tell loom what happened to a memory it served (`memory.outcome`): the feedback loop. */
  outcome(
    memoryKey: string,
    outcome: OpInput<"memory.outcome">["outcome"],
    scope: Scope & Omit<Body<"memory.outcome">, "memory_key" | "outcome"> = {},
  ) {
    const [ids, rest] = splitScope(scope);
    return this.call("memory.outcome", { memory_key: memoryKey, outcome, ...rest } as Body<"memory.outcome">, ids);
  }

  /** One budgeted block of everything relevant for a prompt (`context.pack`). */
  contextPack(body: Body<"context.pack"> = {} as Body<"context.pack">, scope: Scope = {}) {
    return this.call("context.pack", body, scope);
  }

  /**
   * Whether this Agent's token may call `op` on its namespace:
   * true / false when known, undefined when the SDK cannot tell — then make the call.
   */
  async canCall(op: string): Promise<boolean | undefined> {
    return (await this.client.capabilities()).canCall(op, this.namespace);
  }

  // ---- what an agent framework holds ---------------------------

  /** A conversation this Agent resumes, appends to and flushes: see `Session`. */
  session(sessionId: string, opts: SessionOptions = {}): Session {
    return new Session(this, sessionId, opts);
  }

  /** Loom's verbs as the model's tools, bound to a scope: see `AgentTools`. */
  tools(scope: Scope = {}, opts: AgentToolsOptions = {}): AgentTools {
    return new AgentTools(this, scope, opts);
  }
}
