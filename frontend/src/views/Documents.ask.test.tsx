// "Ask about this" in the Library's document preview: secondary, before Download; it closes
// the preview first, with no focus handed back to its opener, then the assistant panel
// opens with the document in its chip and the focus in its field.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { useDock } from "../lib/assistantDock";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { AssistantPanel } from "../layout/AssistantPanel";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  getKnowledge: vi.fn(),
  fetchKnowledgePreview: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));

const ROW = {
  source_uri: "notes/billing.md",
  title: "Billing notes",
  original_filename: "",
  tags: [],
  has_blob: false,
};

async function settle() {
  for (let i = 0; i < 3; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
}

describe("Ask about this in the document preview", () => {
  beforeEach(() => {
    resetNamespaceRoleCache();
    api.listKnowledge.mockResolvedValue({ rows: [ROW], total: 1, next_cursor: null });
    api.getKnowledge.mockResolvedValue({
      found: true,
      content: "Invoices go out on Fridays.",
      source_uri: ROW.source_uri,
    });
    useStore.setState({
      authed: true,
      whoami: { authenticated: true, subject: "v", org: "acme", roles: ["VIEWER"], namespaces: ["team"] },
      myNamespaces: [{ namespace: "team", role: "viewer", status: "active", kind: "memory" } as never],
      identity: { org: "acme", namespace: "team", agent: "loom-ui" },
      view: "knowledge",
    });
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
    history.replaceState(null, "", "/library");
    const real = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
        String(input).startsWith("/v1/health/ready")
          ? new Response(JSON.stringify({ ready: true, checks: { assistant: { ok: true } } }))
          : real(input, init),
      ),
    );
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it("closes the preview, then the panel opens on its field with the document in the chip", async () => {
    render(
      <>
        <Documents />
        <AssistantPanel />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Preview Billing notes" }));
    await settle();
    const preview = screen.getByRole("dialog", { name: "Billing notes" });
    const ask = within(preview).getByRole("button", { name: "Ask the assistant about this document" });
    expect(ask).toHaveTextContent("Ask about this");
    // Secondary, just before the primary Download.
    const actions = within(preview)
      .getAllByRole("button")
      .map((b) => b.getAttribute("aria-label") || b.textContent);
    expect(actions.indexOf("Ask the assistant about this document")).toBe(actions.indexOf("Download text (.md)") - 1);
    await userEvent.setup().click(ask);
    await waitFor(() => expect(screen.getByLabelText("Question")).toHaveFocus(), { timeout: 3000 });
    expect(screen.queryByRole("dialog", { name: "Billing notes" })).not.toBeInTheDocument();
    expect(useDock.getState().item).toMatchObject({
      view: "knowledge",
      chip: "document Billing notes",
      line: 'document notes/billing.md ("Billing notes")',
    });
    expect(screen.getByTestId("assistant-page-chip")).toHaveTextContent("Includes: Library · document Billing notes");
  });
});
