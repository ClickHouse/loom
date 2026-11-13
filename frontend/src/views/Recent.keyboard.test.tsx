import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MemoryListEntry } from "../api";
import { useStore } from "../lib/state";
import { MemoryInspector } from "../components/MemoryInspector";
import { Recent } from "./Recent";

// The arrows move the real focus from card to card, so Enter opens the focused card and
// closing the inspector puts focus back on it. No one-letter key does anything.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(),
    getMemory: vi.fn(),
    memoryHistory: vi.fn(async () => ({ rows: [] })),
    memoryRelated: vi.fn(async () => ({ results: [] })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({ found: false })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});
import { getMemory, listMemories } from "../api";

const entry = (key: string): MemoryListEntry => ({
  memory_key: key,
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: new Date().toISOString(),
  expires_at: null,
  pinned: false,
  tags: [],
  value: `value of ${key}`,
});

const identity = { org: "dev", namespace: "ns-a", agent: "loom-ui" };

// Recent with the inspector App mounts beside it.
function Page() {
  const key = useStore((s) => s.inspectedMemoryKey);
  const inspect = useStore((s) => s.inspect);
  return (
    <>
      <Recent />
      {key && (
        <MemoryInspector identity={identity} memoryKey={key} onClose={() => inspect(null)} onChanged={() => {}} />
      )}
    </>
  );
}

const openButton = (key: string) => screen.getByRole("button", { name: new RegExp(`^${key}`) });

describe("Memories keyboard", () => {
  beforeEach(() => {
    vi.mocked(listMemories).mockResolvedValue({
      entries: [entry("one/key"), entry("two/key"), entry("three/key")],
      total: 3,
      truncated: false,
      today: 3,
    });
    vi.mocked(getMemory).mockImplementation(
      async (_id, key): Promise<MemoryDetail> => ({
        found: true,
        value: `value of ${key}`,
        memory_type: "semantic",
        confidence: 1,
        version: 1,
        pinned: false,
        created_at: "2026-09-18T09:00:00Z",
        tags: [],
      }),
    );
    useStore.setState({
      authed: true,
      whoami: { authenticated: false },
      identity,
      view: "memories",
      inspectedMemoryKey: null,
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, identity: null, inspectedMemoryKey: null });
  });

  it("moves focus with the arrows, opens the focused card with Enter and returns to it on Escape", async () => {
    render(<Page />);
    await screen.findByText("two/key");
    await userEvent.keyboard("{ArrowDown}");
    expect(openButton("one/key")).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(openButton("two/key")).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(useStore.getState().inspectedMemoryKey).toBe("two/key"));
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(openButton("two/key")).toHaveFocus();
  });

  it("starts from the focused card, and ArrowUp moves back", async () => {
    render(<Page />);
    await screen.findByText("three/key");
    openButton("two/key").focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(openButton("three/key")).toHaveFocus();
    await userEvent.keyboard("{ArrowUp}{ArrowUp}");
    expect(openButton("one/key")).toHaveFocus();
  });

  it("toggles the focused card's selection with Space", async () => {
    render(<Page />);
    await screen.findByText("one/key");
    await userEvent.keyboard("{ArrowDown} ");
    expect(screen.getByRole("checkbox", { name: "Select one/key" })).toBeChecked();
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
  });

  it("leaves the arrow keys to a focused control outside the list", async () => {
    render(<Page />);
    await screen.findByText("one/key");
    const add = screen.getByRole("button", { name: "Add memory" });
    add.focus();
    await userEvent.keyboard("{ArrowDown}");
    // A menu trigger opens its menu on ArrowDown; the list must not take focus from it.
    expect(add).toHaveFocus();
  });

  it("skips a key a focused widget already handled", async () => {
    render(<Page />);
    await screen.findByText("one/key");
    openButton("one/key").focus();
    const handled = new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true });
    handled.preventDefault();
    document.activeElement?.dispatchEvent(handled);
    expect(openButton("one/key")).toHaveFocus();
  });

  // The dashboard has no keyboard shortcuts: j and / leave the list and its search alone,
  // and the drawer's e, d, p and Ctrl/⌘ Enter do nothing (its buttons do the work).
  it("takes no one-letter key on the list or in the drawer", async () => {
    render(<Page />);
    await screen.findByText("one/key");
    await userEvent.keyboard("j/");
    expect(document.body).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}{Enter}");
    const drawer = await screen.findByRole("dialog");
    await within(drawer).findAllByText("value of one/key");
    drawer.focus();
    await userEvent.keyboard("edp");
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("button", { name: "Save new version" })).toBeNull();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });
});
