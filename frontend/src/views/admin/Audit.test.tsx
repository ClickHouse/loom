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
    expect(await screen.findByText(/by alice/)).toBeTruthy();
    expect(screen.getByText(/by bob/)).toBeTruthy();
    expect(screen.getByText(/by carol/)).toBeTruthy();
    expect(screen.getAllByText("Created namespace (private)")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: /Details/ })).toBeNull();
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
  it("announces how many events it shows in an always-mounted status region", async () => {
    vi.mocked(api.listAuditLog).mockResolvedValueOnce({
      rows: [row("alice", "{}"), row("bob", "{}"), row("bob", "{}")],
    });
    render(<Audit />);
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("3 events");
    await userEvent.setup().type(screen.getByRole("textbox", { name: "Filter audit events" }), "alice");
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("1 event");
  });
});
