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
// Items added in one millisecond keep their order: a per-process counter on top of the clock.
let tick = 0n;

/**
 * The OpenAI Agents SDK's `Session` over a loom `Agent` (the twin of the Python
 * `LoomSession`): the conversation history of one session, as episodic memories of
 * the user's loom session, keyed `<keyPrefix>/<nanoseconds>-<random>` so keys sort
 * in the order the items were added. The Python and TypeScript sessions of the same
 * id and user read the same history.
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
      throw new Error(`userId names a loom user or is undefined; ${JSON.stringify(user)} names none`);
    }
    const prefix = (opts.keyPrefix ?? "openai-agents").replace(/^\/+|\/+$/g, "");
    if (!prefix) throw new Error("keyPrefix must not be empty");
    this.prefix = `${prefix}/`;
  }

  async getSessionId(): Promise<string> {
    return this.sessionId;
  }

  private scope(): Promise<Scope> {
    // loom files a long value's chunks by session alone, and a caller chooses its ids.
    this.loomSession ??= sha256Hex(JSON.stringify([this.opts.userId ?? "-", this.sessionId])).then(
      (hex) => `oai-${hex.slice(0, 20)}`,
    );
    return this.loomSession.then((sessionId) => ({ userId: this.opts.userId, sessionId }));
  }

  private async rows(): Promise<Array<{ memory_key: string; value?: unknown }>> {
    const scope = await this.scope();
    const rows: Array<{ memory_key: string; value?: unknown }> = [];
    for (;;) {
      const page = await this.agent.call(
        "memory.list",
        {
          prefix: this.prefix,
          memory_type: "episodic",
          limit: SESSION_PAGE,
          offset: rows.length,
          fields: ["memory_key", "value"],
        },
        scope,
      );
      rows.push(...(page.entries as Array<{ memory_key: string; value?: unknown }>));
      if (!page.truncated) return rows;
    }
  }

  async getItems(limit?: number): Promise<AgentInputItem[]> {
    const rows = await this.rows();
    const kept = limit === undefined ? rows : limit > 0 ? rows.slice(-limit) : [];
    return kept.map((row) => item(row.value));
  }

  async addItems(items: AgentInputItem[]): Promise<void> {
    const scope = await this.scope();
    for (const entry of items) {
      // One memory.set per item: a batch folds a value identical to a live row.
      const stamp = (BigInt(Date.now()) * 1_000_000n + (tick++ % 1_000_000n)).toString().padStart(20, "0");
      await this.agent.call(
        "memory.set",
        { memory_key: `${this.prefix}${stamp}-${randomHex()}`, value: JSON.stringify(entry), memory_type: "episodic" },
        scope,
      );
    }
  }

  async popItem(): Promise<AgentInputItem | undefined> {
    const rows = await this.rows();
    const last = rows.at(-1);
    if (!last) return undefined;
    await this.agent.call("memory.delete", { memory_key: last.memory_key }, await this.scope());
    return item(last.value);
  }

  async clearSession(): Promise<void> {
    const scope = await this.scope();
    for (const row of await this.rows()) {
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
