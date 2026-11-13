/**
 * Loom as tools for the AI SDK (`ai`).
 *
 * `loomTools` returns Loom's agent verbs (`loom_remember`, `loom_recall`,
 * `loom_ask`, `loom_offload`, `loom_fetch`, `loom_note`) as an AI SDK
 * `ToolSet`, the record `generateText` and `streamText` take as `tools`, so the
 * model keeps and reads long-term memory in Loom during a call:
 *
 *     const { text } = await generateText({ model, tools: loomTools(bot, { userId: "alice" }), prompt });
 *
 * The tools are `AgentTools` (the same names, schemas and results as
 * `agent.tools()`), bound to one scope. `ai` is an optional peer dependency.
 */

import { jsonSchema, type ToolSet, tool } from "ai";
import type { Agent, Scope } from "../agent.js";
import { AgentTools, type AgentToolsOptions } from "../tools.js";

/** Loom's agent verbs as an AI SDK `ToolSet`, bound to one user, session or run. */
export function loomTools(agent: Agent, scope: Scope = {}, opts: AgentToolsOptions = {}): ToolSet {
  const tools = new AgentTools(agent, scope, opts);
  return Object.fromEntries(
    tools.definitions().map((spec) => [
      spec.name,
      tool({
        description: spec.description,
        // The same JSON schema: Loom's is readonly, jsonSchema() takes mutable arrays.
        inputSchema: jsonSchema(spec.parameters as unknown as Parameters<typeof jsonSchema>[0]),
        // A thrown LoomError reaches the model as the AI SDK's tool-error result, with Loom's message.
        execute: (input: unknown) => tools.execute(spec.name, input),
      }),
    ]),
  );
}
