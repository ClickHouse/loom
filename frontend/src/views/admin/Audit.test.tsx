/** An audit row's subject is JSON text from the server; one that parses to something other
 *  than an object ("null", an array, garbage) renders as a row with no details, never throws. */
import { act, cleanup, render, screen, waitFor, within, fireEvent } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import type { AuditEntry } from "../../api";
import { useStore } from "../../lib/state";
import { Audit } from "./Audit";
import { ApiError } from "../../lib/errors";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listAuditLog: vi.fn(async () => ({ rows: [] })),
    listErasures: vi.fn(async () => ({ rows: [] })),
  };
});

const row = (actor: string, subject: string, event_type = "namespace.register"): AuditEntry => ({
  ts: new Date().toISOString(),
  event_type,
  actor,
  subject,
  request_id: "",
});

beforeEach(() => {
  useStore.setState({ identity: { org: "dev", namespace: "ns1", agent: "loom-ui" } });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Audit rows", () => {
  it("summarises an object subject and offers its details", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [row("alice", '{"namespace":"team","visibility":"org"}')],
    });
    render(<Audit />);
    expect(await screen.findByText("Created namespace team (org)")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Details/ })).toBeTruthy();
  });

  it("renders a subject that is JSON null, an array or garbage as an empty subject", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [row("alice", "null"), row("bob", '["team"]'), row("carol", "{not json")],
    });
    render(<Audit />);
    expect(await screen.findAllByText("Created namespace (private)")).toHaveLength(3);
    const table = screen.getByRole("table", { name: "Audit events" });
    for (const actor of ["alice", "bob", "carol"]) expect(within(table).getByText(actor)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Details/ })).toBeNull();
  });

  it("reads a hashed text value as no value", async () => {
    const hashed = '{"sha256":"ab12","chars":31}';
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [
        row("alice", `{"memory_key":"k1","reason":${hashed}}`, "memory.delete"),
        row("bob", `{"memory_key":"k2","reason":"stale"}`, "memory.delete"),
        row("carol", `{"memory_key":"k3","outcome":${hashed}}`, "memory.outcome"),
      ],
    });
    render(<Audit />);
    expect(await screen.findByText('Deleted memory "k1"')).toBeTruthy();
    expect(screen.getByText('Deleted "k2" — stale')).toBeTruthy();
    expect(screen.getByText('Recorded outcome for "k3"')).toBeTruthy();
    expect(screen.queryByText(/object Object/)).toBeNull();
  });

  it("says when Loom retired memories on its own, and when one was restored", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [
        row("process:loom", '{"reason":"conflict_resolver","count":3,"retired":[]}', "memory.auto_supersede"),
        row("process:loom", '{"reason":"value_update","count":1,"retired":[]}', "memory.auto_supersede"),
        row("alice", '{"memory_key":"user_city","memory_id":"m1"}', "memory.unsupersede"),
      ],
    });
    render(<Audit />);
    expect(
      await screen.findByText(
        "Loom retired 3 memories automatically (replaced by a later statement of the same attribute)",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Loom retired 1 memory automatically (replaced by an updated value)")).toBeTruthy();
    expect(screen.getByText('Restored "user_city" after it was retired')).toBeTruthy();
  });

  it("does not count a retirement the re-read could not confirm as done", async () => {
    const unsure = '{"memory_id":"m2","unconfirmed":"true"}';
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [
        row(
          "process:loom",
          `{"reason":"newest_wins_contradiction","count":1,"retired":[${unsure}]}`,
          "memory.auto_supersede",
        ),
        row(
          "process:loom",
          `{"reason":"newest_wins_contradiction","count":3,"retired":[{"memory_id":"m1"},${unsure},{"memory_id":"m3"}]}`,
          "memory.auto_supersede",
        ),
      ],
    });
    render(<Audit />);
    expect(
      await screen.findByText(
        "Loom may have retired 1 memory automatically (replaced by a newer write (namespace policy))",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Loom retired 2 memories automatically (replaced by a newer write (namespace policy)), and may have retired 1 more",
      ),
    ).toBeTruthy();
  });
});

describe("Audit filter", () => {
  // The toolbar's live count is the one region that says it (ListToolbar); a second sr-only
  // status of the page's own said every count twice. While loading, the only status is
  // the table's skeleton.
  it("announces how many events it shows from the toolbar's live count alone", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [row("alice", "{}"), row("bob", "{}"), row("bob", "{}")],
    });
    render(<Audit />);
    const live = () =>
      document.querySelector<HTMLElement>('search[aria-label="Filter audit events"] [aria-live="polite"]');
    const region = live();
    expect(region).not.toBeNull();
    expect(screen.getAllByRole("status").map((s) => s.getAttribute("aria-label"))).toEqual(["Loading audit events"]);
    await waitFor(() => expect(region).toHaveTextContent("3 events"));
    expect(screen.queryAllByRole("status")).toEqual([]);
    await userEvent.setup().type(screen.getByRole("textbox", { name: "Filter audit events" }), "alice");
    expect(live()).toBe(region);
    expect(region).toHaveTextContent("1 event");
  });

  it("counts the events at the end of the toolbar, and nowhere else", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({ rows: [row("alice", "{}"), row("bob", "{}")] });
    render(<Audit />);
    await screen.findAllByText("Created namespace (private)");
    const bar = document.querySelector<HTMLElement>('search[aria-label="Filter audit events"]');
    expect(bar).toHaveTextContent("2 events");
    expect(within(bar!).getByRole("textbox", { name: "Filter audit events" })).toBeInTheDocument();
  });
});

describe("Audit counts over a partial load", () => {
  it("counts the loaded events with a + and says a filter covers only those", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [row("alice", "{}"), row("bob", "{}")],
      next_cursor: "c1",
    });
    render(<Audit />);
    await screen.findAllByText("Created namespace (private)");
    const bar = document.querySelector<HTMLElement>('search[aria-label="Filter audit events"]');
    expect(bar).toHaveTextContent("2+ events");
    await userEvent.setup().type(screen.getByRole("textbox", { name: "Filter audit events" }), "carol");
    expect(await screen.findByText(/No loaded events match/)).toBeInTheDocument();
  });
});

describe("Audit failure", () => {
  it("says a failed read failed, with its request id and Retry, never 'No audit events'", async () => {
    vi.mocked(api.listAuditLog).mockRejectedValueOnce(
      new ApiError({ status: 503, message: "audit store down", requestId: "req-3" }),
    );
    render(<Audit />);
    expect(await screen.findByText("Couldn't load audit events")).toBeInTheDocument();
    expect(screen.getByText(/req-3/)).toBeInTheDocument();
    expect(screen.queryByText("No audit events")).toBeNull();
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({ rows: [row("alice", '{"namespace":"team"}')] });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/Created namespace team/)).toBeInTheDocument();
  });

  it("says a failed erasures read failed, with Retry", async () => {
    vi.mocked(api.listErasures).mockRejectedValueOnce(new ApiError({ status: 500, message: "boom" }));
    render(<Audit />);
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Erasures/ }));
    expect(await screen.findByText("Couldn't load erasures")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No erasures yet")).toBeInTheDocument();
  });
});

describe("Audit table", () => {
  it("draws the first read as the table's header over a skeleton, marked busy", async () => {
    vi.mocked(api.listAuditLog).mockReturnValueOnce(new Promise(() => {}));
    render(<Audit />);
    const table = await screen.findByRole("table", { name: "Audit events" });
    expect(table).toHaveAttribute("aria-busy", "true");
    // The first read waits 300 ms before its loader shows, like every loader
    // (ui/LoadingSkeleton), under the header row already in place.
    expect(within(table).queryByText("Loading data")).toBeNull();
    expect(within(table).getByRole("status", { name: "Loading audit events" })).toBeInTheDocument();
  });

  it("draws the erasures' first read as the table's header over a skeleton", async () => {
    vi.mocked(api.listErasures).mockReturnValueOnce(new Promise(() => {}));
    render(<Audit />);
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Erasures/ }));
    const table = await screen.findByRole("table", { name: "Erasures" });
    expect(within(table).queryByText("Loading data")).toBeNull();
    expect(within(table).getByRole("status", { name: "Loading erasures" })).toBeInTheDocument();
  });

  it("puts each event in a row under Event, By and When headers", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [row("alice", '{"namespace":"team","visibility":"org"}')],
    });
    render(<Audit />);
    await screen.findByText("Created namespace team (org)");
    const table = screen.getByRole("table", { name: "Audit events" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((h) => h.textContent),
    ).toEqual(["Event", "By", "When"]);
    const cells = within(within(table).getAllByRole("row")[1]).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("Created namespace team (org)");
    expect(cells[1]).toHaveTextContent("alice");
    expect(within(cells[2]).getByText("just now").tagName).toBe("TIME");
  });
});

// A namespace's history is longer than one read: "Load more" asks for the next, older page
// with the cursor the last one answered, and adds it under the rows already shown.
describe("Audit: older events", () => {
  it("loads the next page under the first, and stops at the last", async () => {
    vi.mocked(api.listAuditLog)
      .mockResolvedValueOnce({ rows: [row("alice", '{"namespace":"team","visibility":"org"}')], next_cursor: "c1" })
      .mockResolvedValueOnce({ rows: [row("bob", '{"namespace":"ops","visibility":"org"}')], next_cursor: null });
    render(<Audit />);
    await screen.findByText("Created namespace team (org)");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Created namespace ops (org)")).toBeTruthy();
    expect(screen.getByText("Created namespace team (org)")).toBeTruthy();
    expect(vi.mocked(api.listAuditLog).mock.calls[1][1]).toMatchObject({ cursor: "c1" });
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  // A namespace switch while an older page is in flight: the answer belongs to the old
  // namespace, so it is dropped, its request aborted, and the new list's button is idle.
  it("drops an older page that lands after the namespace changed", async () => {
    let land: (v: Awaited<ReturnType<typeof api.listAuditLog>>) => void = () => {};
    vi.mocked(api.listAuditLog)
      .mockResolvedValueOnce({ rows: [row("alice", '{"namespace":"team","visibility":"org"}')], next_cursor: "c1" })
      .mockImplementationOnce(() => new Promise((resolve) => (land = resolve)))
      .mockResolvedValueOnce({ rows: [row("carol", '{"namespace":"two","visibility":"org"}')], next_cursor: "c2" });
    render(<Audit />);
    await screen.findByText("Created namespace team (org)");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    const signal = vi.mocked(api.listAuditLog).mock.calls[1][1]?.signal;
    act(() => useStore.setState({ identity: { org: "dev", namespace: "ns2", agent: "loom-ui" } }));
    await screen.findByText("Created namespace two (org)");
    expect(signal?.aborted).toBe(true);
    await act(async () => land({ rows: [row("bob", '{"namespace":"ops","visibility":"org"}')], next_cursor: null }));
    expect(screen.queryByText("Created namespace ops (org)")).toBeNull();
    expect(screen.getByRole("button", { name: "Load more" })).not.toHaveAttribute("aria-busy");
  });

  // The button unmounts with the last page; focus goes to the first row it added, and stays
  // on the button while more remain.
  it("keeps focus on the button while more remain, then moves it to the first new row", async () => {
    vi.mocked(api.listAuditLog)
      .mockResolvedValueOnce({ rows: [row("alice", '{"namespace":"team","visibility":"org"}')], next_cursor: "c1" })
      .mockResolvedValueOnce({ rows: [row("bob", '{"namespace":"ops","visibility":"org"}')], next_cursor: "c2" })
      .mockResolvedValueOnce({ rows: [row("dave", '{"namespace":"last","visibility":"org"}')], next_cursor: null });
    render(<Audit />);
    await screen.findByText("Created namespace team (org)");
    const button = screen.getByRole("button", { name: "Load more" });
    button.focus();
    fireEvent.click(button);
    await screen.findByText("Created namespace ops (org)");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Load more" })));
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Created namespace last (org)");
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByText("Created namespace last (org)").closest("tr")),
    );
  });

  // The reader moved on while the last page read: the focus stays where they put it.
  it("leaves the focus where the reader moved it while the last page read", async () => {
    let answer: (v: { rows: AuditEntry[]; next_cursor: null }) => void = () => {};
    vi.mocked(api.listAuditLog)
      .mockResolvedValueOnce({ rows: [row("alice", '{"namespace":"team","visibility":"org"}')], next_cursor: "c1" })
      .mockImplementationOnce(() => new Promise((r) => (answer = r)));
    render(<Audit />);
    await screen.findByText("Created namespace team (org)");
    const button = screen.getByRole("button", { name: "Load more" });
    button.focus();
    fireEvent.click(button);
    const filter = screen.getByRole("textbox", { name: "Filter audit events" });
    filter.focus();
    await act(async () =>
      answer({ rows: [row("dave", '{"namespace":"last","visibility":"org"}')], next_cursor: null }),
    );
    await screen.findByText("Created namespace last (org)");
    expect(filter).toHaveFocus();
  });

  // The last page holds only system events, hidden by default: no new row to focus, so the
  // focus goes to the last row shown rather than falling to the page.
  it("moves focus to the last row shown when every row of the last page is hidden", async () => {
    const system: AuditEntry = { ...row("worker", "{}"), event_type: "worker.failed" };
    vi.mocked(api.listAuditLog)
      .mockResolvedValueOnce({ rows: [row("alice", '{"namespace":"team","visibility":"org"}')], next_cursor: "c1" })
      .mockResolvedValueOnce({ rows: [system, system], next_cursor: null });
    render(<Audit />);
    await screen.findByText("Created namespace team (org)");
    const button = screen.getByRole("button", { name: "Load more" });
    button.focus();
    fireEvent.click(button);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Load more" })).toBeNull());
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByText("Created namespace team (org)").closest("tr")),
    );
  });
});
