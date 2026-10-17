/** An audit row's subject is JSON text from the server; one that parses to something other
 *  than an object ("null", an array, garbage) renders as a row with no details, never throws. */
import { cleanup, render, screen, within, fireEvent } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import type { AuditEntry } from "../../api";
import { useStore } from "../../lib/state";
import { Audit } from "./Audit";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listAuditLog: vi.fn(async () => ({ rows: [] })),
    listErasures: vi.fn(async () => ({ rows: [] })),
  };
});

const row = (actor: string, subject: string): AuditEntry => ({
  ts: new Date().toISOString(),
  event_type: "namespace.register",
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
});

describe("Audit filter", () => {
  it("announces how many events it shows in an always-mounted status region", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [row("alice", "{}"), row("bob", "{}"), row("bob", "{}")],
    });
    render(<Audit />);
    // The count's status region is the one on the page once rows land.
    await screen.findByText("3 events");
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("3 events");
    await userEvent.setup().type(screen.getByRole("textbox", { name: "Filter audit events" }), "alice");
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("1 event");
  });
});

describe("Audit table", () => {
  it("draws the first read as Click UI's loading table, marked busy", async () => {
    vi.mocked(api.listAuditLog).mockReturnValueOnce(new Promise(() => {}));
    render(<Audit />);
    const table = await screen.findByRole("table", { name: "Audit events" });
    expect(table).toHaveAttribute("aria-busy", "true");
    expect(within(table).getByText("Loading data")).toBeInTheDocument();
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
  });
});

// A namespace's history is longer than one read: "Load older" asks for the next, older page
// with the cursor the last one answered, and adds it under the rows already shown.
describe("Audit: older events", () => {
  it("loads the next page under the first, and stops at the last", async () => {
    vi.mocked(api.listAuditLog)
      .mockResolvedValueOnce({ rows: [row("alice", '{"namespace":"team","visibility":"org"}')], next_cursor: "c1" })
      .mockResolvedValueOnce({ rows: [row("bob", '{"namespace":"ops","visibility":"org"}')], next_cursor: null });
    render(<Audit />);
    await screen.findByText("Created namespace team (org)");
    fireEvent.click(screen.getByRole("button", { name: "Load older" }));
    expect(await screen.findByText("Created namespace ops (org)")).toBeTruthy();
    expect(screen.getByText("Created namespace team (org)")).toBeTruthy();
    expect(vi.mocked(api.listAuditLog).mock.calls[1][1]).toMatchObject({ cursor: "c1" });
    expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
  });
});
