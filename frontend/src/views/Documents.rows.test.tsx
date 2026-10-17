// The Library's row template carries what a row shows. Its four icon actions (download,
// edit details, edit content, delete) are rendered only for the row under the pointer,
// focused, or selected — otherwise 50 rows would carry ~30 nodes each for controls nobody
// could use at once.
import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
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
          : { rows: [], entries: [], total: 0, next_cursor: null, status: "ok" };
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

const actionButtons = () =>
  screen.queryAllByRole("button", { name: /^(Download original file|Edit details|Edit content|Delete document)$/ });

describe("Library rows", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useStore.setState({ identity: null });
  });

  it("renders no per-row icon actions until a row is hovered, then only that row's", async () => {
    stubNetwork();
    useStore.setState({ identity: { org: ORG, namespace: NS, agent: "loom-ui" }, view: "knowledge" });
    const { container } = render(<Documents />);
    await settle();
    expect(screen.getAllByText(/^Handbook \d+$/).length).toBe(ROWS);
    expect(actionButtons().length).toBe(0);

    const row = must(screen.getByText("Handbook 7").closest("[class*='rounded-xl']"), "Handbook 7 row");
    fireEvent.mouseEnter(row);
    expect(actionButtons().length).toBe(4);
    expect(row.contains(actionButtons()[0])).toBe(true);
    fireEvent.mouseLeave(row);
    expect(actionButtons().length).toBe(0);

    // The whole list stays under the node budget (with every row's actions it is ~5,400
    // nodes at 50 rows; target < 2,500).
    expect(container.querySelectorAll("*").length).toBeLessThan(2500);
  });
});
