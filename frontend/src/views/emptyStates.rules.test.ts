import { describe, expect, it } from "vitest";

// An empty list says, in plain words, what will appear there and what fills it
// (docs/features/dashboard.md): no op name, header or "via MCP" in an empty state's text.
// This reads every `EmptyState` and `ListState kind="empty"` in the page sources, from its
// opening tag to its close at the same indentation.

const SOURCES = import.meta.glob<string>(["../views/**/*.tsx", "../components/**/*.tsx", "!../**/*.test.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
});

const BANNED =
  /\b(run\.begin|run\.complete|knowledge\.search|namespace\.register|memory\.set|conversation\.append|task\.create|task\.pull|inbox\.register)\b|via MCP|X-Loom-/;

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
  it("name no op, header or transport", () => {
    const hits = Object.entries(SOURCES).flatMap(([path, text]) =>
      emptyStates(text)
        .filter(({ block }) => BANNED.test(block))
        .map(({ line, block }) => `${path}:${line}: ${BANNED.exec(block)?.[0]}`),
    );
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
    expect(hits).toEqual([]);
  });

  it("finds the empty states it reads", () => {
    const count = Object.values(SOURCES).reduce((n, text) => n + emptyStates(text).length, 0);
    expect(count).toBeGreaterThan(20);
  });
});
