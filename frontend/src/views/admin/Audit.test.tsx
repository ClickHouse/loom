/** An audit row's subject is JSON text from the server; one that parses to something other
 *  than an object ("null", an array, garbage) renders as a row with no details, never throws. */
import { cleanup, render, screen } from "../../test/rtl";
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
    expect(await screen.findByText(/by alice/)).toBeTruthy();
    expect(screen.getByText(/by bob/)).toBeTruthy();
    expect(screen.getByText(/by carol/)).toBeTruthy();
    expect(screen.getAllByText("Created namespace (private)")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: /Details/ })).toBeNull();
  });
});

describe("Audit filter", () => {
  it("announces how many events it shows in an always-mounted status region", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [row("alice", "{}"), row("bob", "{}"), row("bob", "{}")],
    });
    render(<Audit />);
    // The loading skeleton is a status region too; the count's is the one left once rows land.
    await screen.findByText("3 events");
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("3 events");
    await userEvent.setup().type(screen.getByRole("textbox", { name: "Filter audit events" }), "alice");
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("1 event");
  });
});
