// The Library's row template carries what a row shows. Its actions sit in one More menu
// per row, always visible: the menu's items are rendered only once it is
// opened, so 50 rows carry one button each, not four.
import { act, cleanup, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Documents } from "./Documents";
import { must } from "../test/must";

const ORG = "acme";
const NS = "team";
const ROWS = 50;

function knowledgeRows() {
  return Array.from({ length: ROWS }, (_, i) => ({
    source_uri: `docs/handbook-${i}.md`,
    title: `Handbook ${i}`,
    original_filename: `handbook-${i}.md`,
    description: "",
    tags: ["ops", "runbook"],
    chunks: 3,
    bytes: 4096,
    has_blob: true,
    created_at: "2026-09-19T00:00:00Z",
    updated_at: "2026-09-19T00:00:00Z",
    content_type: "text/markdown",
  }));
}

function stubNetwork() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
      const body =
        path === "/v1/knowledge.list"
          ? { rows: knowledgeRows(), total: ROWS, next_cursor: null }
          : { rows: [], entries: [], sources: [], total: 0, next_cursor: null, status: "ok" };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
}

async function settle() {
  for (let i = 0; i < 3; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
}

describe("Library rows", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useStore.setState({ identity: null });
  });

  it("gives each row one More button and renders no item until a menu opens", async () => {
    stubNetwork();
    useStore.setState({ identity: { org: ORG, namespace: NS, agent: "loom-ui" }, view: "knowledge" });
    const { container } = render(<Documents />);
    await settle();
    expect(screen.getAllByText(/^Handbook \d+$/).length).toBe(ROWS);
    expect(screen.getAllByRole("button", { name: /^Actions for / })).toHaveLength(ROWS);
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
    const row = must(screen.getByText("Handbook 7").closest("[data-doc-row]"), "Handbook 7 row");
    expect(row).toContainElement(screen.getByRole("button", { name: "Actions for Handbook 7" }));

    // The whole list stays under the node budget (with every row's four icon actions it
    // was ~5,400 nodes at 50 rows; target < 2,500).
    expect(container.querySelectorAll("*").length).toBeLessThan(2500);
  });
});
