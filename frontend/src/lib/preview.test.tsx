// A card computes a bounded preview, never a pretty-print of the whole value that is
// re-parsed to detect an envelope.
import { cleanup, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { previewValue } from "./format";
import { detectEncodedRecord, detectEncodedValue } from "./encoded";
import { CARD_VALUE_CHARS, MemoryCard } from "../components/MemoryCard";
import type { MemoryListEntry } from "../api";
import { must } from "../test/must";

const entry: MemoryListEntry = {
  memory_key: "alerts/2026-09-19/1",
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: "2026-09-19T00:00:00Z",
  expires_at: null,
  pinned: false,
  tags: [],
  agent: "bot",
};
const identity = { org: "acme", namespace: "team", agent: "loom-ui" };

describe("previewValue", () => {
  it("returns a string as is, bounded", () => {
    expect(previewValue("hello")).toBe("hello");
    expect(previewValue("x".repeat(1000), 10)).toBe("x".repeat(10));
  });
  it("serialises an object compactly and bounds it", () => {
    const v = { alert: "y".repeat(5000), severity: "high" };
    const p = previewValue(v, 64);
    expect(p.length).toBe(64);
    expect(p.startsWith('{"alert":"yyy')).toBe(true);
    expect(p).not.toContain("\n"); // no pretty-print
  });
  it("is empty for null and tolerant of the unserialisable", () => {
    expect(previewValue(null)).toBe("");
    const cyc: Record<string, unknown> = {};
    cyc.self = cyc;
    expect(previewValue(cyc, 20)).toBe("[object Object]");
  });
});

describe("detectEncodedRecord", () => {
  const payload = btoa("The quick brown fox jumps over the lazy dog. ".repeat(4));
  it("finds the envelope on a parsed object without re-serialising it", () => {
    const enc = detectEncodedRecord({ encoding: "base64", payload });
    expect(enc?.kind).toBe("envelope");
    expect(enc?.decoded.startsWith("The quick brown fox")).toBe(true);
  });
  it("agrees with the string detector on the same envelope", () => {
    const obj = { encoding: "base64", payload };
    expect(detectEncodedRecord(obj)?.decoded).toBe(detectEncodedValue(JSON.stringify(obj))?.decoded);
  });
  it("is null for a plain object, an array, or a scalar", () => {
    expect(detectEncodedRecord({ a: 1 })).toBeNull();
    expect(detectEncodedRecord([payload])).toBeNull();
    expect(detectEncodedRecord("x")).toBeNull();
  });
});

describe("MemoryCard preview", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows a bounded preview of a large object without pretty-printing it", async () => {
    const stringify = vi.spyOn(JSON, "stringify");
    const value = { alert: "z".repeat(4000), n: 1 };
    render(<MemoryCard identity={identity} entry={entry} onSelect={() => {}} active={false} value={value} />);
    // An object reads as its pairs, still cut to two lines' worth.
    const shown = await screen.findByText(/^alert: zzz/);
    // two lines' worth, cut before the clamp
    expect(must(shown.textContent, "preview text").length).toBeLessThanOrEqual(CARD_VALUE_CHARS);
    // no `JSON.stringify(v, null, 2)` anywhere on the card's path
    expect(stringify.mock.calls.some((c) => c.length >= 3 && c[2] === 2)).toBe(false);
  });

  it("ends a value the list cut in a cut mark, and shows one that fits whole", async () => {
    // The server cuts a value to the preview_chars the list asks for (a card's worth) and
    // adds "…": one character past what a card shows.
    const server = render(
      <MemoryCard
        identity={identity}
        entry={entry}
        onSelect={() => {}}
        active={false}
        value={`${"x".repeat(CARD_VALUE_CHARS)}…`}
      />,
    );
    const marked = must((await screen.findByText(/^xxx/)).textContent, "preview text");
    expect(marked.endsWith("…")).toBe(true);
    expect(marked.length).toBeLessThanOrEqual(CARD_VALUE_CHARS);
    server.unmount();
    // A value one character longer than a card shows, with no mark, ends in one too.
    const cut = "word ".repeat(100).slice(0, CARD_VALUE_CHARS + 1);
    const { unmount } = render(
      <MemoryCard identity={identity} entry={entry} onSelect={() => {}} active={false} value={cut} />,
    );
    const shown = must((await screen.findByText(/^word word/)).textContent, "preview text");
    expect(shown.endsWith("…")).toBe(true);
    expect(shown.length).toBeLessThanOrEqual(CARD_VALUE_CHARS);
    unmount();
    const whole = "word ".repeat(100).slice(0, CARD_VALUE_CHARS);
    render(<MemoryCard identity={identity} entry={entry} onSelect={() => {}} active={false} value={whole} />);
    expect(must((await screen.findByText(/^word word/)).textContent, "preview text")).toBe(whole);
  });

  it("flags an agent-encoded envelope from the parsed object", async () => {
    const payload = btoa("Decoded envelope text that is long enough to count as a payload.".repeat(2));
    render(
      <MemoryCard
        identity={identity}
        entry={entry}
        onSelect={() => {}}
        active={false}
        value={{ encoding: "base64", payload }}
      />,
    );
    expect(await screen.findByText("b64")).toBeInTheDocument();
    expect(await screen.findByText(/Decoded envelope text/)).toBeInTheDocument();
  });

  it("keeps the layout containment on a standalone card", () => {
    render(<MemoryCard identity={identity} entry={entry} onSelect={() => {}} active={false} value="plain" />);
    const card = screen.getByRole("button", { name: /alerts\/2026-09-19\/1/ }).closest("[data-memory-key]");
    // the row class carries `content-visibility: auto` (styles.css .list-row)
    expect(card?.className).toContain("list-row");
  });
});
