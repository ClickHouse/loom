import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry } from "../api";
import { axeViolations } from "../test/axe";
import { MemoryCard } from "./MemoryCard";

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
    await userEvent.click(screen.getByRole("button", { name: "Filter by tag: coffee" }));
    expect(onTagClick).toHaveBeenCalledWith("coffee");
    expect(onSelect).not.toHaveBeenCalled();

    const open = screen.getByRole("button", { name: /prefs\/coffee/ });
    expect(open.querySelector("button")).toBeNull();
    open.focus();
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith(entry);
    expect(await axeViolations(container)).toEqual([]);
  });
});
