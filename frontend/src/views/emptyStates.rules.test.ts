import { describe, expect, it } from "vitest";
import { OP_MIN_ROLE } from "../lib/canCall";

// An empty list says, in plain words, what will appear there and what fills it
// (docs/features/dashboard.md): no operation name, wire field, header, environment variable
// or "via MCP" in an empty state's text. What this scans: the source lines of each
// `EmptyState` and `ListState kind="empty"` tag that starts its own line, from that line to
// the tag's close at the same indentation, as literal text. Text a page builds in a variable
// outside the tag, or a tag sharing its line with other code, is outside it.
//
// The operation names are every op the registry serves: the SDK's generated table
// (`sdk/typescript/src/generated/ops.ts`, written by `scripts/codegen/sdk_ts_gen.py` from
// `server.operations.registry`), read as `lib/canCall.test.ts` reads it, together with the
// ops the dashboard gates on (`OP_MIN_ROLE`), which is all a frontend-only checkout has.

const SOURCES = import.meta.glob<string>(["../views/**/*.tsx", "../components/**/*.tsx", "!../**/*.test.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
});

const GENERATED_OPS = new URL("../../../sdk/typescript/src/generated/ops.ts", import.meta.url).pathname;

async function opNames(): Promise<{ names: Set<string>; fromRegistry: boolean }> {
  const names = new Set(Object.keys(OP_MIN_ROLE).filter((op) => op.includes(".")));
  // vitest runs on node; the frontend tsconfig carries no node types (see canCall.test.ts).
  type Fs = { existsSync(p: string): boolean; readFileSync(p: string, enc: "utf8"): string };
  const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as Fs;
  if (!fs.existsSync(GENERATED_OPS)) return { names, fromRegistry: false };
  for (const m of fs.readFileSync(GENERATED_OPS, "utf8").matchAll(/\{ name: "([a-z0-9_.]+)",/g)) names.add(m[1]);
  return { names, fromRegistry: true };
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Lines an empty state may still say, by file and exact text: the Repositories page's line
 *  for whoever runs the deployment (and no one else), naming the variable that lists
 *  repositories. Each is taken out of its block before the scan, so the rest of it is read. */
const ALLOWED: { file: string; text: string }[] = [{ file: "/Code.tsx", text: "List them in LOOM_CODE_REPOS_JSON." }];

/** Each empty state's source: the opening tag's line to its `/>` or closing tag. */
function emptyStates(text: string): { line: number; block: string }[] {
  const lines = text.split("\n");
  const found: { line: number; block: string }[] = [];
  lines.forEach((l, i) => {
    const open = /^\s*<(EmptyState|ListState)\b/.exec(l);
    if (!open) return;
    const indent = l.length - l.trimStart().length;
    const at = (m: string) => m.length - m.trimStart().length === indent;
    // One line (`<ListState kind="error" … />`), else the close at the tag's own indentation.
    const end = l.trimEnd().endsWith("/>")
      ? i
      : lines.findIndex((m, j) => j > i && at(m) && (m.trim() === "/>" || m.trim().startsWith(`</${open[1]}>`)));
    const block = lines.slice(i, end === -1 ? undefined : end + 1).join("\n");
    if (open[1] === "ListState" && !/kind="empty"/.test(block)) return;
    found.push({ line: i + 1, block });
  });
  return found;
}

describe("empty states", () => {
  it("name no operation, wire field, header, variable or transport", async () => {
    const { names, fromRegistry } = await opNames();
    // A checkout with the SDK tree reads the whole registry, not only the dashboard's gates.
    if (fromRegistry) expect(names.size).toBeGreaterThan(300);
    const banned = new RegExp(
      `(?<![\\w.])(${[...names].map(escape).join("|")})(?![\\w])|\\brun_id\\b|\\bLOOM_[A-Z_]+|via MCP|X-Loom-`,
      "g",
    );
    const hits = Object.entries(SOURCES).flatMap(([path, text]) =>
      emptyStates(text).flatMap(({ line, block }) => {
        const read = ALLOWED.filter((a) => path.endsWith(a.file)).reduce((b, a) => b.split(a.text).join(""), block);
        return [...read.matchAll(banned)].map((m) => `${path}:${line}: ${m[0]}`);
      }),
    );
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
    expect(hits).toEqual([]);
  });

  it("finds the empty states it reads, the allowed operator line among them", () => {
    const blocks = Object.entries(SOURCES).flatMap(([path, text]) => emptyStates(text).map((b) => ({ path, ...b })));
    expect(blocks.length).toBeGreaterThan(20);
    for (const a of ALLOWED)
      expect(
        blocks.some((b) => b.path.endsWith(a.file) && b.block.includes(a.text)),
        a.text,
      ).toBe(true);
  });
});
