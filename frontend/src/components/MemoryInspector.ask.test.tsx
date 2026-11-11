import { useState } from "react";
import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { useDock } from "../lib/assistantDock";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { AssistantPanel } from "../layout/AssistantPanel";
import { MemoryInspector } from "./MemoryInspector";

// "Ask about this" in the memory drawer: it closes the drawer, handing the focus to nobody,
// and the assistant panel opens with the memory in its chip and the focus in its field.
// It stays in the footer while an edit is open, unavailable until the edit is done.

const DETAIL: MemoryDetail = {
  found: true,
  value: "the public API allows 100 requests a minute",
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  pinned: false,
  created_at: "2026-09-18T09:00:00Z",
  updated_at: "2026-09-18T09:00:00Z",
  tags: [],
  status: "active",
  agent: "claude-desktop",
};

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getMemory: vi.fn(async () => DETAIL),
    memoryHistory: vi.fn(async () => ({ rows: [] })),
    memoryRelated: vi.fn(async () => ({ results: [] })),
    relatedFacts: vi.fn(async () => ({ entity: "", related: [] })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({ found: false, links: [] })),
    listMemories: vi.fn(async () => ({ entries: [], total: 0 })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
  };
});
vi.mock("./SessionBriefPanel", () => ({ SessionBriefPanel: () => null }));

const writer: WhoAmI = {
  authenticated: true,
  subject: "w@example.com",
  org: "dev",
  roles: ["WRITER"],
  namespaces: ["ns"],
};
const mine: MyNamespaceEntry[] = [
  {
    namespace: "ns",
    role: "writer",
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
    kind: "memory",
  } as MyNamespaceEntry,
];
const identity = { org: "dev", namespace: "ns", agent: "loom-ui" };

/** A card that opens the drawer, the drawer while it is open, and the assistant panel. */
function Page() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open the memory
      </button>
      {open && (
        <MemoryInspector
          identity={identity}
          memoryKey="api/rate-limit"
          onClose={() => setOpen(false)}
          onChanged={() => {}}
        />
      )}
      <AssistantPanel />
      <p id="loom-assistant-status" role="status" />
    </>
  );
}

async function openDrawer() {
  const user = userEvent.setup();
  render(<Page />);
  await user.click(screen.getByRole("button", { name: "Open the memory" }));
  await screen.findByText("the public API allows 100 requests a minute");
  return user;
}

const footer = () => within(screen.getByRole("contentinfo"));

describe("Ask about this in the memory drawer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    sessionStorage.clear();
    useStore.setState({ authed: true, whoami: writer, myNamespaces: mine, identity, view: "memories" });
    useDock.setState({
      open: false,
      mounted: false,
      draft: "",
      item: null,
      pageOff: false,
      viewport: 1440,
      reason: null,
      reasonShort: "",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ ready: true, checks: { assistant: { ok: true } } }))),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("sits after Edit, before More, and names the memory", async () => {
    await openDrawer();
    const names = footer()
      .getAllByRole("button")
      .map((b) => b.getAttribute("aria-label") || b.textContent);
    expect(names.slice(0, 3)).toEqual(["Edit", "Ask the assistant about this memory", "More actions"]);
    expect(footer().getByRole("button", { name: "Ask the assistant about this memory" })).toHaveTextContent(
      "Ask about this",
    );
  });

  it("closes the drawer, opens the panel with the memory in the chip and the focus in its field", async () => {
    const user = await openDrawer();
    await user.click(footer().getByRole("button", { name: "Ask the assistant about this memory" }));
    await waitFor(() => expect(screen.getByLabelText("Question")).toHaveFocus(), { timeout: 3000 });
    expect(screen.queryByText("the public API allows 100 requests a minute")).not.toBeInTheDocument();
    expect(useDock.getState().open).toBe(true);
    expect(useDock.getState().item).toMatchObject({
      view: "memories",
      namespace: "ns",
      chip: "key api/rate-limit",
      line: "memory api/rate-limit (agent claude-desktop)",
    });
    expect(screen.getByTestId("assistant-page-chip")).toHaveTextContent("Includes: Memories · key api/rate-limit");
    // The card that opened the drawer did not take the focus back.
    expect(screen.getByRole("button", { name: "Open the memory" })).not.toHaveFocus();
  });

  it("stays in view while an edit is open, unavailable until it is saved or cancelled", async () => {
    const user = await openDrawer();
    await user.click(footer().getByRole("button", { name: "Edit" }));
    const value = screen.getByRole("textbox", { name: "Memory value" });
    await user.clear(value);
    await user.type(value, "a new value");
    const ask = footer().getByRole("button", { name: "Ask the assistant about this memory" });
    expect(ask).toHaveAttribute("aria-disabled", "true");
    expect(ask).toHaveAccessibleDescription("Save or cancel the edit first");
    await user.click(ask);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByRole("textbox", { name: "Memory value" })).toHaveValue("a new value");
    expect(useDock.getState().open).toBe(false);
    expect(useDock.getState().item).toBeNull();
  });

  it("is unavailable, with the reason, while the assistant cannot answer", async () => {
    useDock.setState({ reason: { kind: "not_configured" }, reasonShort: "not set up" });
    await openDrawer();
    const ask = footer().getByRole("button", { name: "Ask the assistant about this memory" });
    expect(ask).toHaveAttribute("aria-disabled", "true");
    expect(ask).toHaveAccessibleDescription("The assistant can't answer now: not set up.");
  });

  it("is not offered to a role the assistant's route does not admit", async () => {
    useStore.setState({ myNamespaces: [] });
    await openDrawer();
    expect(footer().queryByRole("button", { name: "Ask the assistant about this memory" })).toBeNull();
  });
});
