/**
 * Loom as tools for the OpenAI Agents SDK (the TypeScript twin of
 * `loom.integrations.openai_agents`).
 *
 * `loomTools` returns Loom's agent verbs (`loom_remember`, `loom_recall`,
 * `loom_ask`, `loom_offload`, `loom_fetch`, `loom_note`) as the SDK's function
 * tools, so a model keeps and reads long-term memory in Loom during a run:
 *
 *     const assistant = new Agent({ name: "assistant", tools: loomTools(bot, { userId: "alice" }) });
 *     const result = await run(assistant, "Book my usual seat to Lisbon");
 *
 * The tools are `AgentTools` (the same names, schemas and results as
 * `agent.tools()`), bound to one scope. `@openai/agents` is an optional peer
 * dependency.
 */

import { type FunctionTool, tool } from "@openai/agents";
import type { Agent, Scope } from "../agent.js";
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
        // The same JSON schema; the SDK types its arrays readonly, the framework mutable.
        parameters: spec.parameters as unknown as JsonParameters,
        // Strict mode requires every property; the optional ones (top_k, memory_type) are not.
        strict: false,
        execute: (input) => tools.execute(spec.name, input),
      }) as FunctionTool,
  );
}
