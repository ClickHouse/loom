/**
 * Loom as tools for Mastra agents.
 *
 * `loomTools` returns Loom's agent verbs (`loom_remember`, `loom_recall`,
 * `loom_ask`, `loom_offload`, `loom_fetch`, `loom_note`) as Mastra tools built
 * with `createTool`, keyed by name as a Mastra `Agent` takes them, so the model
 * keeps and reads long-term memory in Loom during a run:
 *
 *     const assistant = new Agent({ id: "assistant", name: "assistant", model, instructions, tools: loomTools(bot, { userId: "alice" }) });
 *
 * The tools are `AgentTools` (the same names, schemas and results as
 * `agent.tools()`), bound to one scope. `@mastra/core` is an optional peer
 * dependency.
 */

import { createTool } from "@mastra/core/tools";
import type { Agent, Scope } from "../agent.js";
import { AgentTools, type AgentToolsOptions } from "../tools.js";

/** The schema a Mastra tool takes; Loom's JSON schema is one of its forms. */
type InputSchema = NonNullable<Parameters<typeof createTool>[0]["inputSchema"]>;

/** Loom's agent verbs as Mastra tools, keyed by tool name, bound to one user, session or run. */
export function loomTools(
  agent: Agent,
  scope: Scope = {},
  opts: AgentToolsOptions = {},
): Record<string, ReturnType<typeof createTool>> {
  const tools = new AgentTools(agent, scope, opts);
  return Object.fromEntries(
    tools.definitions().map((spec) => [
      spec.name,
      createTool({
        id: spec.name,
        description: spec.description,
        // The same JSON schema: Loom's is readonly, createTool() takes mutable arrays.
        inputSchema: spec.parameters as unknown as InputSchema,
        // A thrown LoomError reaches the model as Mastra's failed tool result, with Loom's message.
        execute: (input: unknown) => tools.execute(spec.name, input),
      }),
    ]),
  );
}
