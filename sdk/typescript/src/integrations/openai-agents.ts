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
 * message to the model. `@openai/agents` is an optional peer dependency.
 */

import { type FunctionTool, tool } from "@openai/agents";
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
