/** Fixed read-only code requests and whole-evidence result budgeting. */
import { LoomError } from "./errors.js";
import type { ToolSpec } from "./tools.js";

export const CODE_TOOL_SPEC = {
  name: "loom_code_lookup",
  description:
    "Investigate indexed code first for definitions, behavior, callers and change impact, including bug reports quoting identifiers or errors. Use symbol for named definitions (prefer qualified names or path:line), search for concepts, callers for incoming calls, impact for prospective effects. Use local tools for explicit literal-search requests, uncommitted/unindexed files, or insufficient/abstaining results; reuse sufficient supplied evidence. Honor ambiguity, elision, client_truncation and tier/lower-bound warnings. Client omissions mean incomplete evidence even if server epistemic is exact. An empty graph does not prove no callers. Skip unrelated lookups.",
  parameters: {
    type: "object",
    properties: {
      kind: {
        type: "string",
        enum: ["symbol", "search", "callers", "impact"],
        description:
          "symbol: definition; search: conceptual behavior; callers: incoming calls; impact: prospective effects",
      },
      target: {
        type: "string",
        description: "Qualified symbol or path:line for symbol/callers/impact; a natural-language question for search",
      },
      depth: {
        type: "integer",
        minimum: 1,
        maximum: 3,
        description: "Caller hops: callers 1..3 (default 1), impact 1..2 (default 2); omit for symbol/search",
      },
    },
    required: ["kind", "target"],
    additionalProperties: false,
  },
} as const satisfies ToolSpec;

export type CodeOp = "code.symbol" | "code.search" | "code.callers" | "code.impact";

// Compact Python/C++ names (including templates/operators) and repo-relative path:line.
const REFERENCE = /^[\w$~][\w$~.:/<>,()[\]*&+=!%|^-]*$/;

export function codeRequest(a: Readonly<Record<string, string | number>>): {
  op: CodeOp;
  body: Record<string, unknown>;
} {
  const kind = a.kind as "symbol" | "search" | "callers" | "impact";
  const target = String(a.target).trim();
  if (!target) throw new LoomError("target must be a non-empty string");
  const depth = a.depth ?? (kind === "impact" ? 2 : 1);
  if (a.depth !== undefined && (kind === "symbol" || kind === "search"))
    throw new LoomError("depth applies only to callers and impact");
  if (kind === "impact" && Number(depth) > 2) throw new LoomError("impact depth must be within 1..2");
  if (kind !== "search" && !REFERENCE.test(target))
    throw new LoomError("target must be a symbol reference or path:line; use search for prose");
  const body: Record<string, unknown> = { include_lessons: false };
  if (kind === "search") Object.assign(body, { query: target, top_k: 3, token_budget: 2500 });
  else {
    body.name = target;
    if (kind === "symbol")
      Object.assign(body, {
        token_budget: 2500,
        include_body: true,
        include_history: false,
        include_examples: false,
        uses: false,
        uses_brief: false,
      });
    else if (kind === "callers") Object.assign(body, { depth, max_nodes: 20 });
    else Object.assign(body, { depth, per_hop_limit: 20 });
  }
  return { op: `code.${kind}`, body };
}

const ROW_KEYS = [
  "reverts",
  "owners",
  "co_change_files",
  "impacted_tests",
  "documented_in",
  "dispatch_seeds",
  "candidates",
  "impacted_symbols",
  "edges",
  "results",
  "definitions",
];

function dump(value: unknown): string {
  return JSON.stringify(value);
}

export function fitCodeResult(result: Record<string, unknown>, limit: number): string {
  let out = structuredClone(result);
  let text = dump(out);
  if (text.length <= limit) return text;
  const cut = {
    reason: "max_result_chars",
    insufficient: true,
    omitted_bodies: 0,
    omitted_rows: {} as Record<string, number>,
  };
  out.client_truncation = cut;
  const data = out.data as Record<string, unknown>;
  for (const key of ["results", "definitions"]) {
    const rows = data[key] as Array<Record<string, unknown>> | undefined;
    for (const row of [...(rows ?? [])].reverse()) {
      if (row.body) {
        Object.assign(row, { body: "", elided: true });
        cut.omitted_bodies += 1;
        text = dump(out);
        if (text.length <= limit) return text;
      }
    }
  }
  for (const key of ROW_KEYS) {
    const rows = data[key];
    if (!Array.isArray(rows)) continue;
    while (rows.length) {
      rows.pop();
      cut.omitted_rows[key] = (cut.omitted_rows[key] ?? 0) + 1;
      text = dump(out);
      if (text.length <= limit) return text;
    }
  }
  // If uncertainty metadata itself cannot fit, partial evidence cannot stand in for it.
  out = { ...result, client_truncation: { reason: "max_result_chars", insufficient: true, metadata_omitted: true } };
  delete out.data;
  text = dump(out);
  return text.length <= limit ? text : dump({ client_truncation: out.client_truncation });
}
