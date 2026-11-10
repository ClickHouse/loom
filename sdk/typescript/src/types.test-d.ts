/**
 * Type-level contract: what the generated surface must
 * infer. These assertions run under `vitest --typecheck`; a change that
 * breaks inference fails CI without a runtime test noticing.
 */
import { describe, expectTypeOf, it } from "vitest";
import type { Agent, Body } from "./agent.js";
import type { AssistantEvent, AssistantSupport } from "./assistant.js";
import type { GroupClient, LoomClient, LoomClientOptions, OpInput, OpOutput } from "./client.js";
import type { MemoryGetOutput, MemorySearchInput, MemorySearchOutput, OpName, OpSpec } from "./generated/ops.js";
import type { CallResult, RetryOverride } from "./index.js";
import type { Resumed, Session } from "./session.js";
import { type AnthropicToolDefinition, type OpenAIToolDefinition, type ToolSpec, toolDefinitions } from "./tools.js";

declare const loom: LoomClient;
declare const agent: Agent;

describe("LoomClient", () => {
  it("call is typed by the op name", () => {
    expectTypeOf(loom.call("memory.search", {} as MemorySearchInput)).resolves.toEqualTypeOf<MemorySearchOutput>();
    expectTypeOf(loom.callWithResponse("memory.search", {} as MemorySearchInput)).resolves.toEqualTypeOf<
      CallResult<MemorySearchOutput>
    >();
    expectTypeOf<OpInput<"memory.search">>().toEqualTypeOf<MemorySearchInput>();
    expectTypeOf<OpOutput<"memory.get">>().toEqualTypeOf<MemoryGetOutput>();
    // @ts-expect-error an op the registry does not have
    loom.call("memory.nope", {});
    // @ts-expect-error query must be a string
    loom.call("memory.search", { org: "o", namespace: "n", query: 42 });
    // @ts-expect-error org and namespace are required on the raw input
    loom.call("memory.search", { query: "q" });
  });

  it("group accessors carry the same types as call", () => {
    expectTypeOf(loom.memory.search).parameter(0).toEqualTypeOf<MemorySearchInput>();
    expectTypeOf(loom.memory.search).returns.resolves.toEqualTypeOf<MemorySearchOutput>();
    expectTypeOf(loom.memory).toEqualTypeOf<GroupClient<"memory">>();
    expectTypeOf(loom.memory.bulkSet).toBeFunction();
    // @ts-expect-error snake_case names are not the surface; the method is camelCase
    loom.memory.bulk_set;
    // A multi-part op name camel-cases across its dots, as the runtime method does.
    expectTypeOf(loom.tracker.sourceAdd).parameter(0).toEqualTypeOf<OpInput<"tracker.source.add">>();
    expectTypeOf(loom.ops.projectionFailuresList).toBeFunction();
    // @ts-expect-error not a group
    loom.nope;
  });

  it("the retry policy is set only through retry", () => {
    expectTypeOf<LoomClientOptions>().toHaveProperty("retry");
    // @ts-expect-error there is no top-level retry shorthand
    expectTypeOf<LoomClientOptions["maxAttempts"]>();
  });

  it("spec and OPS are read-only", () => {
    expectTypeOf(loom.spec("memory.get")).toEqualTypeOf<OpSpec | undefined>();
    expectTypeOf<OpName>().toMatchTypeOf<string>();
    expectTypeOf<RetryOverride>().toHaveProperty("maxAttempts");
  });
});

describe("Agent", () => {
  it("Body strips the identity the Agent stamps but keeps agent optional", () => {
    expectTypeOf<Body<"memory.search">>().not.toHaveProperty("org");
    expectTypeOf<Body<"memory.search">>().not.toHaveProperty("namespace");
    expectTypeOf<Body<"memory.search">>().toHaveProperty("query").toEqualTypeOf<string>();
    expectTypeOf<Body<"memory.search">>().toHaveProperty("agent").toEqualTypeOf<string | undefined>();
    expectTypeOf(agent.recall("q")).resolves.toEqualTypeOf<MemorySearchOutput>();
    expectTypeOf(agent.remember).parameter(0).toEqualTypeOf<string>();
    // @ts-expect-error a per-call scope uses camelCase keys
    agent.recall("q", { user_id: "u" });
  });

  it("a Session resumes to a system prompt and typed transcript turns", () => {
    expectTypeOf(agent.session("t")).toEqualTypeOf<Session>();
    expectTypeOf(agent.session("t").resume()).resolves.toEqualTypeOf<Resumed>();
    expectTypeOf(agent.session("t").resume()).resolves.toHaveProperty("system").toEqualTypeOf<string>();
    // @ts-expect-error a turn's role is one of four
    agent.session("t").append({ role: "narrator", content: "x" });
    expectTypeOf(agent.tools().execute("loom_ask", { query: "q" })).resolves.toEqualTypeOf<string>();
  });

  it("tool definitions are typed by provider and tool names are a closed set", () => {
    expectTypeOf(toolDefinitions("anthropic")).toEqualTypeOf<AnthropicToolDefinition[]>();
    expectTypeOf(toolDefinitions("openai")).toEqualTypeOf<OpenAIToolDefinition[]>();
    expectTypeOf(toolDefinitions()).toEqualTypeOf<
      ToolSpec<"loom_remember" | "loom_recall" | "loom_ask" | "loom_offload" | "loom_fetch" | "loom_note">[]
    >();
    expectTypeOf(agent.tools().definitions("openai")).toEqualTypeOf<OpenAIToolDefinition[]>();
    // @ts-expect-error not one of the six tools
    agent.tools({}, { only: ["loom_forget"] });
  });
});

describe("assistant", () => {
  it("an answer's support is AssistantSupport", () => {
    expectTypeOf<Extract<AssistantEvent, { type: "answer" }>["answer"]["support"]>().toEqualTypeOf<AssistantSupport>();
  });
});
