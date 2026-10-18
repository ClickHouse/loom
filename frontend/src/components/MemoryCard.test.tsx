import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry } from "../api";
import { axeViolations } from "../test/axe";
import { MemoryCard, valuePairs } from "./MemoryCard";

const entry: MemoryListEntry = {
  memory_key: "prefs/coffee",
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: "2026-09-19T00:00:00Z",
  expires_at: null,
  pinned: true,
  tags: ["coffee", "morning"],
  agent: "bot",
};
const identity = { org: "acme", namespace: "team", agent: "loom-ui" };

describe("MemoryCard", () => {
  afterEach(() => cleanup());

  it("opens on the card and filters on a tag, with no control inside another", async () => {
    const onSelect = vi.fn();
    const onTagClick = vi.fn();
    const { container } = render(
      <MemoryCard
        identity={identity}
        entry={entry}
        onSelect={onSelect}
        onTagClick={onTagClick}
        active={false}
        value="oat milk"
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Filter by tag coffee" }));
    expect(onTagClick).toHaveBeenCalledWith("coffee");
    expect(onSelect).not.toHaveBeenCalled();

    const open = screen.getByRole("button", { name: /prefs\/coffee/ });
    expect(open.querySelector("button")).toBeNull();
    open.focus();
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith(entry);
    expect(await axeViolations(container)).toEqual([]);
  });

  // The open button is named by the key alone; the agent, the time and the value describe it.
  it("is named by its key and described by its state, writer, time and value", () => {
    render(<MemoryCard identity={identity} entry={entry} onSelect={() => {}} active={false} value="oat milk" />);
    const open = screen.getByRole("button", { name: "prefs/coffee" });
    expect(open).toHaveAccessibleDescription(/pinned.*bot.*UTC.*oat milk/);
    expect(open).not.toHaveAccessibleDescription(/prefs\/coffee/);
  });

  // A row that opens in place has no chevron, and its time is the list
  // time ("3d ago", "Sep 20") with the UTC time behind it, not a clock time without a day.
  it("shows the list time and no chevron", () => {
    vi.useFakeTimers({ now: Date.parse("2026-09-28T15:00:00Z"), toFake: ["Date"] });
    try {
      const { container } = render(
        <MemoryCard identity={identity} entry={entry} onSelect={() => {}} active={false} value="oat milk" />,
      );
      const time = container.querySelector("time");
      expect(time).toHaveTextContent("Sep 19");
      expect(time).toHaveAttribute("dateTime", "2026-09-19T00:00:00.000Z");
      // The pinned star is the row's one icon.
      expect(screen.getByRole("button", { name: /prefs\/coffee/ }).querySelectorAll("svg")).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  // A JSON value reads as one muted mono line of `k: v` pairs.
  it("draws a JSON value as its key: value pairs", () => {
    const value = JSON.stringify({ steps: ["drain", "re-point", "replay"], owner: "oncall", sla_minutes: 30 });
    render(<MemoryCard identity={identity} entry={entry} onSelect={() => {}} active={false} value={value} />);
    const line = screen.getByText("steps: drain, re-point, replay · owner: oncall · sla_minutes: 30");
    expect(line).toHaveClass("font-mono", "text-xs", "text-text-muted");
    expect(screen.queryByText(/"steps"/)).toBeNull();
  });

  it("puts the row's aside after the meta, outside the open button", () => {
    render(
      <MemoryCard
        identity={identity}
        entry={entry}
        onSelect={() => {}}
        active={false}
        value="oat milk"
        aside={<button type="button">Why this result</button>}
      />,
    );
    const open = screen.getByRole("button", { name: /prefs\/coffee/ });
    expect(open).not.toContainElement(screen.getByRole("button", { name: "Why this result" }));
  });
});

describe("valuePairs", () => {
  it("reads an object or a JSON object string, and nothing else", () => {
    expect(valuePairs({ a: 1, b: [1, 2], c: { d: true }, e: null })).toBe('a: 1 · b: 1, 2 · c: {"d":true} · e: null');
    expect(valuePairs('{"owner":"oncall"}')).toBe("owner: oncall");
    expect(valuePairs("plain text")).toBeNull();
    expect(valuePairs('{"cut": "off')).toBeNull();
    expect(valuePairs("[1,2]")).toBeNull();
    expect(valuePairs({})).toBeNull();
  });
});
