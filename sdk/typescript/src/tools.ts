/**
 * Loom as the model's tools. Five verbs an LLM can call —
 * remember, recall, ask, offload, fetch — plus a note into working memory,
 * as tool definitions in the shape each provider expects and one
 * `execute()` that runs them against an `Agent` under a scope and returns
 * the string the model reads.
 *
 *     const tools = bot.tools({ userId: "u1", sessionId: "thread-42" });
 *     const defs = tools.definitions("openai");              // or "anthropic" | "generic"
 *     // … model returns a tool call { name, arguments } …
 *     const result = await tools.execute(call.name, call.arguments);
 *
 * The parameter schemas are hand-written subsets of the generated op inputs:
 * what a model should decide (key, value, query), not what the SDK stamps
 * (identity) or the operator tunes (budgets, modes). `execute()` checks the
 * model's arguments against the same schemas before anything is sent.
 */

import type { Agent, Scope } from "./agent.js";
import { LoomError } from "./errors.js";

export type ToolFormat = "anthropic" | "openai" | "generic";

export interface JsonSchema {
  readonly type: "object";
  readonly properties: Readonly<
    Record<
      string,
      {
        readonly type: "string" | "integer";
        readonly description: string;
        readonly enum?: readonly string[];
        readonly minimum?: number;
        readonly maximum?: number;
      }
    >
  >;
  readonly required?: readonly string[];
  readonly additionalProperties?: false;
}

export interface ToolSpec<N extends string = string> {
  readonly name: N;
  readonly description: string;
  readonly parameters: JsonSchema;
}

const SPECS = [
  {
    name: "loom_remember",
    description:
      "Store one fact about the user or the task under a stable key so it can be recalled in later sessions. Use a short snake_case key; the same key updates the fact.",
    parameters: {
      type: "object",
      properties: {
        memory_key: { type: "string", description: "Stable identifier, e.g. seat_preference" },
        value: { type: "string", description: "The fact, one or two sentences, in the third person" },
        memory_type: {
          type: "string",
          description: "semantic (default): a fact; preference: what the user likes; procedural: how to do something",
          enum: ["semantic", "preference", "procedural"],
        },
      },
      required: ["memory_key", "value"],
      additionalProperties: false,
    },
  },
  {
    name: "loom_recall",
    description:
      "Search stored memories by meaning. Returns the closest facts with a similarity score; call before answering anything the user may have told you earlier.",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "What you want to know, as a question or phrase" },
        top_k: { type: "integer", description: "How many facts to return (default 5)", minimum: 1, maximum: 20 },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
  {
    name: "loom_ask",
    description:
      "Ask the memory store a question and get a grounded answer with the memory keys it cites, or an abstention when the memories do not say. Prefer it over loom_recall when you need an answer, not raw facts.",
    parameters: {
      type: "object",
      properties: { query: { type: "string", description: "The question" } },
      required: ["query"],
      additionalProperties: false,
    },
  },
  {
    name: "loom_offload",
    description:
      "Park a large piece of text (a log, a page, a file) outside the conversation and get back a one-line pointer to keep instead. Use for anything over a few hundred lines you may need again.",
    parameters: {
      type: "object",
      properties: {
        content: { type: "string", description: "The text to park" },
        summary: { type: "string", description: "One line saying what it is" },
        blob_key: { type: "string", description: "Optional stable key; omit to address by content" },
      },
      required: ["content", "summary"],
      additionalProperties: false,
    },
  },
  {
    name: "loom_fetch",
    description:
      "Read a parked text back: the lines matching a regex, or a character range. Page with offset until next_offset is -1.",
    parameters: {
      type: "object",
      properties: {
        blob_key: { type: "string", description: "The key from the pointer" },
        pattern: { type: "string", description: "Regex; only matching lines are returned" },
        offset: { type: "integer", description: "Character offset to start from", minimum: 0 },
        limit: { type: "integer", description: "Maximum characters to return", minimum: 1 },
      },
      required: ["blob_key"],
      additionalProperties: false,
    },
  },
  {
    name: "loom_note",
    description:
      "Append a line to the working notes that are loaded at the start of every turn: the current plan, an open question, a decision taken.",
    parameters: {
      type: "object",
      properties: { addition: { type: "string", description: "The line to add" } },
      required: ["addition"],
      additionalProperties: false,
    },
  },
] as const satisfies readonly ToolSpec[];

export type ToolName = (typeof SPECS)[number]["name"];

export const LOOM_TOOL_SPECS: readonly ToolSpec<ToolName>[] = Object.freeze(SPECS);

export interface AnthropicToolDefinition {
  name: ToolName;
  description: string;
  input_schema: JsonSchema;
}

export interface OpenAIToolDefinition {
  type: "function";
  function: { name: ToolName; description: string; parameters: JsonSchema; strict: false };
}

/** A tool definition in the shape `format` names. */
export type ToolDefinition<F extends ToolFormat = ToolFormat> = F extends "anthropic"
  ? AnthropicToolDefinition
  : F extends "openai"
    ? OpenAIToolDefinition
    : ToolSpec<ToolName>;

/** Tool definitions in a provider's shape. */
export function toolDefinitions<F extends ToolFormat = "generic">(
  format?: F,
  only?: readonly ToolName[],
): ToolDefinition<F>[] {
  const specs = LOOM_TOOL_SPECS.filter((s) => !only || only.includes(s.name));
  return specs.map((s) => definition(format ?? "generic", s)) as ToolDefinition<F>[];
}

function definition(format: ToolFormat, s: ToolSpec<ToolName>): ToolDefinition {
  switch (format) {
    case "anthropic":
      return { name: s.name, description: s.description, input_schema: s.parameters };
    case "openai":
      return {
        type: "function",
        function: { name: s.name, description: s.description, parameters: s.parameters, strict: false },
      };
    case "generic":
      return { ...s };
  }
}

export interface AgentToolsOptions {
  only?: readonly ToolName[];
  /** Cap on the string returned to the model. Default 8,000 characters. */
  maxResultChars?: number;
}

/** The tools bound to one Agent and scope. */
export class AgentTools {
  private readonly names: ReadonlySet<ToolName>;
  private readonly maxResultChars: number;

  constructor(
    readonly agent: Agent,
    readonly scope: Scope = {},
    opts: AgentToolsOptions = {},
  ) {
    this.names = new Set(LOOM_TOOL_SPECS.map((s) => s.name).filter((n) => !opts.only || opts.only.includes(n)));
    this.maxResultChars = opts.maxResultChars ?? 8_000;
  }

  definitions<F extends ToolFormat = "generic">(format?: F): ToolDefinition<F>[] {
    return toolDefinitions(format, Array.from(this.names));
  }

  has(name: string): name is ToolName {
    return (this.names as ReadonlySet<string>).has(name);
  }

  /**
   * Run one tool call and return the string to hand back to the model. A call
   * the model got wrong (unknown tool, bad JSON, an argument outside its
   * schema) throws a `LoomError` whose message the model can act on.
   */
  async execute(name: string, args: unknown): Promise<string> {
    const spec = LOOM_TOOL_SPECS.find((s) => s.name === name);
    if (!spec || !this.has(spec.name))
      throw new LoomError(`unknown tool ${name}; this agent offers ${Array.from(this.names).join(", ")}`);
    const out = await this.run(spec.name, checkArgs(spec, args));
    const text = typeof out === "string" ? out : JSON.stringify(out);
    return text.length > this.maxResultChars ? `${text.slice(0, this.maxResultChars)}…[truncated]` : text;
  }

  private async run(name: ToolName, a: Args): Promise<unknown> {
    switch (name) {
      case "loom_remember": {
        const memoryType = optText(a, "memory_type");
        const r = await this.agent.remember(text(a, "memory_key"), text(a, "value"), {
          ...this.scope,
          ...(memoryType ? { memory_type: memoryType } : {}),
        });
        return { stored: true, memory_key: text(a, "memory_key"), version: r.version, verdict: r.conflict_verdict };
      }
      case "loom_recall": {
        const topK = optInt(a, "top_k");
        const r = await this.agent.recall(text(a, "query"), {
          ...this.scope,
          ...(topK !== undefined ? { top_k: topK } : {}),
          // Unpinned, a large top_k serves excerpts (above 15 by default) and drops each hit's explanation.
          bodies: "full",
          explain: false,
        });
        return {
          memories: (r.results ?? []).map((h) => ({
            memory_key: h.memory_key,
            content: h.content_excerpt,
            similarity: Number(h.similarity.toFixed(3)),
          })),
        };
      }
      case "loom_ask": {
        const r = await this.agent.ask(text(a, "query"), this.scope);
        return { answer: r.answer ?? "", abstain: r.abstain ?? false, cited: r.memory_keys ?? [] };
      }
      case "loom_offload": {
        const blobKey = optText(a, "blob_key");
        const r = await this.agent.call(
          "context.offload",
          { content: text(a, "content"), summary: text(a, "summary"), ...(blobKey ? { blob_key: blobKey } : {}) },
          this.scope,
        );
        return r.pointer;
      }
      case "loom_fetch": {
        const [pattern, offset, limit] = [optText(a, "pattern"), optInt(a, "offset"), optInt(a, "limit")];
        const r = await this.agent.call(
          "context.fetch",
          {
            blob_key: text(a, "blob_key"),
            ...(pattern ? { pattern } : {}),
            ...(offset !== undefined ? { offset } : {}),
            ...(limit !== undefined ? { limit } : {}),
          },
          this.scope,
        );
        if (!r.found) return `no parked text under blob_key=${text(a, "blob_key")} (expired or never stored)`;
        return { content: r.content ?? "", next_offset: r.next_offset ?? -1, matched_lines: r.matched_lines };
      }
      case "loom_note": {
        const r = await this.agent.call("working_memory.append", { addition: text(a, "addition") }, this.scope);
        return { noted: true, version: r.version, fill_ratio: r.fill_ratio };
      }
      default: {
        const unreachable: never = name;
        throw new LoomError(`unknown tool ${String(unreachable)}`);
      }
    }
  }
}

/** A tool call's arguments after `checkArgs`: only the properties given, each of its declared type. */
type Args = Readonly<Record<string, string | number>>;

/** The model's arguments checked against the tool's schema; extra keys are ignored. */
function checkArgs(spec: ToolSpec, raw: unknown): Args {
  const given = parseArgs(raw);
  const required = new Set(spec.parameters.required ?? []);
  const out: Record<string, string | number> = {};
  for (const [key, prop] of Object.entries(spec.parameters.properties)) {
    const v = given[key];
    if (v === undefined || v === null || v === "") {
      if (!required.has(key)) continue;
      const kind = prop.type === "string" ? "a non-empty string" : "an integer";
      throw new LoomError(`tool argument ${key} is required and must be ${kind}`);
    }
    if (prop.type === "integer") {
      // Models sometimes quote a number.
      const n = typeof v === "string" && v.trim() !== "" ? Number(v) : v;
      if (typeof n !== "number" || !Number.isInteger(n))
        throw new LoomError(`tool argument ${key} must be an integer, got ${JSON.stringify(v)}`);
      if ((prop.minimum !== undefined && n < prop.minimum) || (prop.maximum !== undefined && n > prop.maximum))
        throw new LoomError(
          `tool argument ${key} must be within ${prop.minimum ?? ""}..${prop.maximum ?? ""}, got ${n}`,
        );
      out[key] = n;
    } else {
      if (typeof v !== "string") throw new LoomError(`tool argument ${key} must be a string, got ${JSON.stringify(v)}`);
      if (prop.enum && !prop.enum.includes(v))
        throw new LoomError(`tool argument ${key} must be one of ${prop.enum.join(", ")}, got ${v}`);
      out[key] = v;
    }
  }
  return out;
}

function parseArgs(args: unknown): Record<string, unknown> {
  let v = args;
  if (typeof args === "string") {
    try {
      v = args.trim() ? JSON.parse(args) : {};
    } catch {
      throw new LoomError("tool arguments are not valid JSON");
    }
  }
  if (v === undefined || v === null) return {};
  if (typeof v !== "object" || Array.isArray(v)) throw new LoomError("tool arguments must be a JSON object");
  return v as Record<string, unknown>;
}

function text(a: Args, key: string): string {
  const v = a[key];
  if (typeof v !== "string") throw new LoomError(`tool argument ${key} is required and must be a non-empty string`);
  return v;
}

function optText(a: Args, key: string): string | undefined {
  const v = a[key];
  return typeof v === "string" ? v : undefined;
}

function optInt(a: Args, key: string): number | undefined {
  const v = a[key];
  return typeof v === "number" ? v : undefined;
}
