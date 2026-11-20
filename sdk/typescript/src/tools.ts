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
 * An explicit `codeNamespace` also offers one read-only code lookup tool.
 * The parameter schemas are hand-written subsets of the generated op inputs:
 * what a model should decide (key, value, query), not what the SDK stamps
 * (identity) or the operator tunes (budgets, modes). `execute()` checks the
 * model's arguments against the same schemas before anything is sent.
 */

import type { Agent, Scope } from "./agent.js";
import type { OpInput } from "./client.js";
import { CODE_TOOL_SPEC, type CodeOp, codeRequest, fitCodeResult } from "./code-tools.js";
import { LoomError } from "./errors.js";
import type { MemorySearchOutput } from "./generated/ops.js";
import { hasIdentityWhitespace } from "./whitespace.js";

const MEMORY_INSUFFICIENT = JSON.stringify({
  insufficient: true,
  metadata_omitted: true,
  reason: "max_result_chars",
});
const RECALL_FIELDS = [
  "abstain",
  "abstain_code",
  "abstain_reason",
  "pending_extraction",
  "degraded",
  "retrieval_confidence",
  "retrieval_risk",
  "term_leg",
  "fallback_budget_exhausted",
  "namespaces_searched",
  "namespaces_skipped",
  "next_page",
  "next",
  "bodies_elided",
] as const;
const RECALL_HIT_FIELDS = [
  "namespace",
  "memory_type",
  "version",
  "status",
  "code_state",
  "source_class",
  "source_resource_id",
  "verified",
  "valid_at",
  "created_at",
  "below_threshold",
  "governed",
  "superseded_by",
  "superseded_at",
  "resolved_from",
  "body_truncated",
  "next",
  "source",
  "parent_memory_key",
  "derived_from",
] as const;

function present(source: object, fields: readonly string[]): Record<string, unknown> {
  return Object.fromEntries(
    fields.filter((key) => key in source).map((key) => [key, (source as Record<string, unknown>)[key]]),
  );
}

function recallResult(r: MemorySearchOutput) {
  return {
    ...present(r, RECALL_FIELDS),
    memories: (r.results ?? []).map((h) => ({
      ...present(h, RECALL_HIT_FIELDS),
      memory_key: h.memory_key,
      content: h.content_excerpt,
      similarity: Number(h.similarity.toFixed(3)),
    })),
  };
}

export type ToolFormat = "anthropic" | "openai" | "generic";

export interface JsonSchema {
  readonly type: "object";
  readonly properties: Readonly<
    Record<
      string,
      {
        readonly type: "string" | "integer" | "array";
        readonly description: string;
        readonly enum?: readonly string[];
        readonly minimum?: number;
        readonly maximum?: number;
        readonly items?: { readonly type: "string" };
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
      "Store one fact about the user or the task under a stable key so it can be recalled in later sessions. Use a stable scoped key; the same key updates the fact. Read the receipt: noop or merged_into_key means no new row; conflicts need resolution.",
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
        files: {
          type: "array",
          items: { type: "string" },
          description:
            "Repository-relative files this fact concerns; [] clears references. A file reference alone does not prove a resolved code link.",
        },
        tags: {
          type: "array",
          items: { type: "string" },
          description: "Classification and repository tags for this fact.",
        },
        source_resource_id: { type: "string", description: "Supporting source path, document identifier or URL." },
      },
      required: ["memory_key", "value"],
      additionalProperties: false,
    },
  },
  {
    name: "loom_recall",
    description:
      "Search stored memories by meaning. Returns the closest facts with a similarity score; call before answering anything the user may have told you earlier. A result too long to return leaves out its lowest-ranked memories and counts them in omitted. Respect abstention, below_threshold, code_state and provenance; verified requires attestation. retrieval_risk and retrieval_confidence apply only to non-governed hits.",
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

const UPDATE_TOOL_SPEC = {
  name: "loom_update",
  description:
    "Correct one existing fact by its saved key in the configured memory scope. Use this for a changed fact instead of writing another key. The server refuses an ambiguous key; do not choose a row or create a replacement silently. Check updated, new_version and the returned content hash before claiming the correction persisted.",
  parameters: {
    type: "object",
    properties: {
      memory_key: { type: "string", description: "Existing stable key returned by recall or a write." },
      value: { type: "string", description: "The corrected fact." },
      if_match_sha256: { type: "string", description: "Known current content hash; a changed row refuses the update." },
    },
    required: ["memory_key", "value"],
    additionalProperties: false,
  },
} as const satisfies ToolSpec;

export type ToolName = (typeof SPECS)[number]["name"] | typeof CODE_TOOL_SPEC.name | typeof UPDATE_TOOL_SPEC.name;

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
  const available = only ? [CODE_TOOL_SPEC, UPDATE_TOOL_SPEC, ...LOOM_TOOL_SPECS] : LOOM_TOOL_SPECS;
  const specs = available.filter((s) => !only || only.includes(s.name));
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
  /** Opt in to read-only code lookup under this application-bound namespace. */
  codeNamespace?: string;
  /** Cap on the string returned to the model. Default 8,000 characters. */
  maxResultChars?: number;
}

/** The tools bound to one Agent and scope. */
export class AgentTools {
  private readonly names: ReadonlySet<ToolName>;
  private readonly maxResultChars: number;
  private readonly codeNamespace: string | undefined;

  constructor(
    readonly agent: Agent,
    readonly scope: Scope = {},
    opts: AgentToolsOptions = {},
  ) {
    this.maxResultChars = opts.maxResultChars ?? 8_000;
    if (opts.only?.includes("loom_update")) namedCorrectionUserId(scope.userId);
    if (opts.only?.includes("loom_code_lookup") && opts.codeNamespace === undefined)
      throw new LoomError("loom_code_lookup requires codeNamespace");
    if (opts.codeNamespace !== undefined) {
      if (typeof opts.codeNamespace !== "string" || !opts.codeNamespace.trim())
        throw new LoomError("codeNamespace must be a non-empty string");
      if (!Number.isInteger(this.maxResultChars) || this.maxResultChars < 256)
        throw new LoomError("code tools require maxResultChars of at least 256");
    }
    this.codeNamespace = opts.codeNamespace?.trim();
    const defaults = this.codeNamespace ? [CODE_TOOL_SPEC, ...LOOM_TOOL_SPECS] : LOOM_TOOL_SPECS;
    const available = opts.only ? [...defaults, UPDATE_TOOL_SPEC] : defaults;
    this.names = new Set(available.map((s) => s.name).filter((n) => !opts.only || opts.only.includes(n)));
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
    const spec = [CODE_TOOL_SPEC, UPDATE_TOOL_SPEC, ...LOOM_TOOL_SPECS].find((s) => s.name === name);
    if (!spec || !this.has(spec.name))
      throw new LoomError(`unknown tool ${name}; this agent offers ${Array.from(this.names).join(", ")}`);
    if (
      (name === "loom_remember" || name === "loom_recall" || name === "loom_update") &&
      this.maxResultChars < MEMORY_INSUFFICIENT.length
    )
      throw new LoomError("maxResultChars is too small to report memory result omissions");
    if (spec.name === "loom_code_lookup") {
      const raw = parseArgs(args);
      const extra = Object.keys(raw).filter((key) => !Object.hasOwn(CODE_TOOL_SPEC.parameters.properties, key));
      if (extra.length) throw new LoomError(`unexpected code tool arguments: ${extra.sort().join(", ")}`);
      const checked = checkArgs(CODE_TOOL_SPEC, raw);
      const depth = optInt(checked, "depth");
      const { op, body } = codeRequest({
        kind: text(checked, "kind"),
        target: text(checked, "target"),
        ...(depth !== undefined ? { depth } : {}),
      });
      const input = { ...this.agent.identity(this.scope), ...body, namespace: this.codeNamespace } as OpInput<CodeOp>;
      const response = await this.agent.client.callWithResponse(op, input);
      return fitCodeResult(
        {
          op,
          request_id: response.requestId,
          scope: { org: this.agent.org, namespace: this.codeNamespace },
          data: response.data,
        },
        this.maxResultChars,
      );
    }
    const out = await this.run(spec.name, checkArgs(spec, args));
    if (name === "loom_remember" || name === "loom_recall" || name === "loom_update")
      return this.fitMemoryResult(out as Record<string, unknown>);
    const resultText = typeof out === "string" ? out : JSON.stringify(out);
    return resultText.length > this.maxResultChars
      ? `${resultText.slice(0, this.maxResultChars)}…[truncated]`
      : resultText;
  }

  /** Keep whole evidence and disclose when even the metadata cannot fit. */
  private fitMemoryResult(out: Record<string, unknown>): string {
    let text = JSON.stringify(out);
    const memories = out.memories;
    if (Array.isArray(memories) && text.length > this.maxResultChars) {
      let omitted = 0;
      while (memories.length && text.length > this.maxResultChars) {
        memories.pop();
        out.omitted = ++omitted;
        text = JSON.stringify(out);
      }
    }
    return text.length <= this.maxResultChars ? text : MEMORY_INSUFFICIENT;
  }

  private async run(name: Exclude<ToolName, "loom_code_lookup">, a: Args): Promise<unknown> {
    switch (name) {
      case "loom_remember": {
        const memoryType = optText(a, "memory_type");
        const source = optText(a, "source_resource_id");
        const r = await this.agent.remember(text(a, "memory_key"), text(a, "value"), {
          ...this.scope,
          ...(memoryType ? { memory_type: memoryType } : {}),
          ...(Array.isArray(a.files) ? { files: a.files } : {}),
          ...(Array.isArray(a.tags) ? { tags: a.tags } : {}),
          ...(source ? { source_resource_id: source } : {}),
        });
        return {
          ...r,
          stored: Boolean(r.memory_id && r.version && !r.noop && !r.dry_run),
          memory_key: r.memory_key || text(a, "memory_key"),
          verdict: r.conflict_verdict || "",
        };
      }
      case "loom_update": {
        const userId = namedCorrectionUserId(this.scope.userId);
        const expected = optText(a, "if_match_sha256");
        return this.agent.client.call("memory.update", {
          org: this.agent.org,
          namespace: this.agent.namespace,
          agent: "*",
          user_id: userId,
          session_id: "-",
          run_id: this.scope.runId ?? "-",
          memory_key: text(a, "memory_key"),
          value: text(a, "value"),
          ...(expected ? { if_match_sha256: expected } : {}),
        });
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
        return recallResult(r);
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

function namedCorrectionUserId(value: unknown): string {
  if (typeof value !== "string" || !value || value === "-" || hasIdentityWhitespace(value))
    throw new LoomError("loom_update requires an application-bound named userId");
  return value;
}

/** A tool call's arguments after `checkArgs`: only the properties given, each of its declared type. */
type Args = Readonly<Record<string, string | number | string[]>>;

/** The model's arguments checked against the tool's schema; extra keys are ignored. */
function checkArgs(spec: ToolSpec, raw: unknown): Args {
  const given = parseArgs(raw);
  const required = new Set(spec.parameters.required ?? []);
  const out: Record<string, string | number | string[]> = {};
  for (const [key, prop] of Object.entries(spec.parameters.properties)) {
    const v = given[key];
    if (v === undefined || v === null || v === "") {
      if (!required.has(key)) continue;
      const kind = prop.type === "string" ? "a non-empty string" : "an integer";
      throw new LoomError(`tool argument ${key} is required and must be ${kind}`);
    }
    if (prop.type === "array") {
      if (!Array.isArray(v) || !v.every((item) => typeof item === "string"))
        throw new LoomError(`tool argument ${key} must be an array of strings`);
      out[key] = v;
    } else if (prop.type === "integer") {
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
