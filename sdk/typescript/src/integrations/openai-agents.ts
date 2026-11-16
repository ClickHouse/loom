/**
 * Loom as tools for the OpenAI Agents SDK (the TypeScript twin of
 * `loom.integrations.openai_agents`).
 *
 * `loomTools` returns Loom's agent verbs (`loom_remember`, `loom_recall`,
 * `loom_ask`, `loom_offload`, `loom_fetch`, `loom_note`) as the OpenAI Agents
 * SDK's function tools, so a model keeps and reads long-term memory in Loom
 * during a run:
 *
 *     const assistant = new Agent({ name: "assistant", tools: loomTools(bot, { userId: "alice" }) });
 *     const result = await run(assistant, "Book my usual seat to Lisbon");
 *
 * The tools are `AgentTools` (the same names, schemas and results as
 * `agent.tools()`), bound to one scope. A call Loom refuses returns its error
 * message to the model. `LoomSession` keeps a run's conversation history in
 * Loom as the SDK's `Session`. `@openai/agents` is an optional peer dependency.
 */

import { type AgentInputItem, type FunctionTool, type Session, tool } from "@openai/agents";
import type { Agent, Scope } from "../agent.js";
import { LoomError } from "../errors.js";
import { AgentTools, type AgentToolsOptions } from "../tools.js";

/** The JSON-schema parameters a non-strict `tool()` takes (the type is not exported). */
type JsonParameters = Extract<Parameters<typeof tool>[0], { strict: false }>["parameters"];

/** Loom's agent verbs as OpenAI Agents SDK tools, bound to one user, session or run. */
export function loomTools(agent: Agent, scope: Scope = {}, opts: AgentToolsOptions = {}): FunctionTool[] {
  const tools = new AgentTools(agent, scope, opts);
  return tools.definitions().map(
    (spec) =>
      tool({
        name: spec.name,
        description: spec.description,
        // The same JSON schema: Loom's is readonly, tool() takes mutable arrays.
        parameters: spec.parameters as unknown as JsonParameters,
        // Strict mode requires every property; the optional ones (top_k, memory_type) are not.
        strict: false,
        execute: async (input) => {
          try {
            return await tools.execute(spec.name, input);
          } catch (e) {
            // The model can fix its call from Loom's message; tool()'s default hides it.
            if (e instanceof LoomError) return `${spec.name} failed: ${e.message}`;
            throw e;
          }
        },
      }) as FunctionTool,
  );
}

export interface LoomSessionOptions {
  /** The end user the history belongs to; unset, the namespace's shared rows. */
  userId?: string;
  /** Key prefix of the items. Default `openai-agents`, as the Python `LoomSession`. */
  keyPrefix?: string;
}

const SESSION_PAGE = 100;
// A key this session wrote, after its prefix: 20 digits of nanoseconds, 8 hex.
const ITEM_KEY = /^\d{20}-[0-9a-f]{8}$/;
// The last stamp this process wrote: the next is the clock or one past it, never less.
let lastStamp = 0n;

type ItemRow = { memory_key: string; value?: unknown; value_truncated?: boolean; created_at?: string };

/**
 * The OpenAI Agents SDK's `Session` over a Loom `Agent` (the twin of the Python
 * `LoomSession`): the conversation history of one session, as episodic memories of the
 * Loom agent and the user, keyed `<keyPrefix>/<nanoseconds>-<random>`; reads sort the
 * items by key, the order they were added. Every process whose Loom agent has the same
 * name reads the same TypeScript history for the same id and user. Python sessions
 * have a separate history because their item formats differ.
 */
export class LoomSession implements Session {
  private readonly prefix: string;
  private loomSession?: Promise<string>;

  constructor(
    private readonly agent: Agent,
    private readonly sessionId: string,
    private readonly opts: LoomSessionOptions = {},
  ) {
    if (!sessionId) throw new Error("sessionId names the session");
    const user = opts.userId;
    if (user !== undefined && ["", "-"].includes(user.trim())) {
      throw new Error(`userId names a Loom user or is undefined; ${JSON.stringify(user)} names none`);
    }
    const prefix = (opts.keyPrefix ?? "openai-agents").replace(/^\/+|\/+$/g, "");
    if (!prefix) throw new Error("keyPrefix must not be empty");
    this.prefix = `${prefix}/`;
  }

  async getSessionId(): Promise<string> {
    return this.sessionId;
  }

  private scope(): Promise<Scope> {
    // Users and SDK languages have separate histories: their native item formats differ.
    this.loomSession ??= sha256Hex(JSON.stringify([this.opts.userId ?? "-", this.sessionId])).then(
      (hex) => `oai-ts-${hex.slice(0, 20)}`,
    );
    return this.loomSession.then((sessionId) => ({ userId: this.opts.userId, sessionId }));
  }

  /**
   * The latest `limit` item rows (all when undefined), oldest first. memory.list serves
   * the newest second first and orders by key within a second, so a read for `limit`
   * rows stops once it holds `limit` and a row of an older second.
   */
  private async latest(limit?: number): Promise<ItemRow[]> {
    if (limit !== undefined && limit <= 0) return [];
    const scope = await this.scope();
    const rows: ItemRow[] = [];
    const seen = new Set<string>();
    let offset = 0;
    for (;;) {
      const page = await this.agent.call(
        "memory.list",
        {
          prefix: this.prefix,
          memory_type: "episodic",
          limit: SESSION_PAGE,
          offset,
          fields: ["memory_key", "value", "value_truncated", "created_at"],
        },
        scope,
      );
      const entries = page.entries as ItemRow[];
      // The server may cap a viewer's page or filter stale projected rows.
      offset += Math.max(1, entries.length);
      for (const row of entries) {
        if (!seen.has(row.memory_key) && ITEM_KEY.test(row.memory_key.slice(this.prefix.length))) {
          seen.add(row.memory_key);
          rows.push(row);
        }
      }
      if (!page.truncated) break;
      if (limit !== undefined && rows.length > limit) {
        if (String(rows[rows.length - 1].created_at) < String(rows[limit - 1].created_at)) break;
      }
    }
    rows.sort((a, b) => (a.memory_key < b.memory_key ? -1 : a.memory_key > b.memory_key ? 1 : 0));
    if (limit === undefined) return rows;
    return limit > 0 ? rows.slice(-limit) : [];
  }

  /** A row's item: the listed value, or the whole one by key when the list cut it (or sent none). */
  private async value(row: ItemRow): Promise<AgentInputItem> {
    if (row.value_truncated || row.value === undefined || row.value === null) {
      const full = await this.agent.call("memory.get", { memory_key: row.memory_key }, await this.scope());
      return item((full as { value?: unknown }).value);
    }
    return item(row.value);
  }

  async getItems(limit?: number): Promise<AgentInputItem[]> {
    const rows = await this.latest(limit);
    const out: AgentInputItem[] = [];
    for (const row of rows) out.push(await this.value(row));
    return out;
  }

  async addItems(items: AgentInputItem[]): Promise<void> {
    const scope = await this.scope();
    for (const entry of items) {
      const clock = BigInt(Date.now()) * 1_000_000n;
      lastStamp = clock > lastStamp ? clock : lastStamp + 1n;
      // One memory.set per item: a batch folds a value identical to a live row.
      await this.agent.call(
        "memory.set",
        {
          memory_key: `${this.prefix}${lastStamp.toString().padStart(20, "0")}-${randomHex()}`,
          value: JSON.stringify(entry),
          memory_type: "episodic",
        },
        scope,
      );
    }
  }

  async popItem(): Promise<AgentInputItem | undefined> {
    const [last] = await this.latest(1);
    if (!last) return undefined;
    const popped = await this.value(last);
    await this.agent.call("memory.delete", { memory_key: last.memory_key }, await this.scope());
    return popped;
  }

  async clearSession(): Promise<void> {
    const scope = await this.scope();
    for (const row of await this.latest()) {
      await this.agent.call("memory.delete", { memory_key: row.memory_key }, scope);
    }
  }
}
/** A stored item: the JSON text it was written as, or the value loom parsed from it. */
function item(value: unknown): AgentInputItem {
  return (typeof value === "string" ? JSON.parse(value) : value) as AgentInputItem;
}

function randomHex(): string {
  const bytes = new Uint8Array(4);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256Hex(text: string): Promise<string> {
  const subtle = (globalThis as { crypto?: { subtle?: SubtleCrypto } }).crypto?.subtle;
  if (!subtle) throw new LoomError("LoomSession needs crypto.subtle (a secure context) to name its loom session");
  const buf = await subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}
