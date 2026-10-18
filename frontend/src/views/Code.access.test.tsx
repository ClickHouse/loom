/**
 * The Access tab's request and reader rows: each time is the list's one time format, and a
 * page of waiting requests has no primary button per row (one primary per surface).
 */
import { cleanup, render, screen, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GrantEntry } from "../api";
import { useStore } from "../lib/state";
import { AccessTab } from "./Code";

const grant = (subject: string, over: Partial<GrantEntry> = {}): GrantEntry => ({
  subject,
  role: "viewer",
  status: "active",
  granted_at: "2025-03-04 10:00:00",
  granted_by: "alice",
  ...over,
});

const listGrants = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listGrants: (...a: unknown[]) => listGrants(...a),
    getNamespace: async () => ({ namespace: "code-app", join_policy: "approval" }),
  };
});
vi.mock("../lib/sharedReads", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sharedReads")>();
  return { ...actual, sharedIndexStatus: async () => ({ org_readable: false }) };
});

beforeEach(() => {
  useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "dev", roles: ["ADMIN"] } });
  listGrants.mockResolvedValue({
    rows: [
      grant("alice", { role: "admin" }),
      grant("bob", { status: "pending", pending_since: "2025-03-05 09:00:00" }),
      grant("carol", { status: "pending", pending_since: "2025-03-06 09:00:00" }),
    ],
  });
});
afterEach(() => cleanup());

describe("AccessTab rows", () => {
  it("draws a request's wait and a reader's grant time with ListTime", async () => {
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} />);
    const waiting = await screen.findByRole("region", { name: "Waiting for approval" });
    const asked = waiting.querySelectorAll("time");
    expect([...asked].map((t) => t.getAttribute("dateTime"))).toEqual([
      "2025-03-05T09:00:00.000Z",
      "2025-03-06T09:00:00.000Z",
    ]);
    const readers = screen.getByRole("region", { name: "Readers" });
    const added = within(readers).getByText(/^added/);
    expect(added.querySelector("time")).toHaveAttribute("dateTime", "2025-03-04T10:00:00.000Z");
    expect(added).toHaveTextContent("by alice");
  });

  // No waiting row has a primary Approve: a list of them would be a column of primaries.
  it("gives no waiting row a primary button", async () => {
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} />);
    const waiting = await screen.findByRole("region", { name: "Waiting for approval" });
    const approves = within(waiting).getAllByRole("button", { name: /^Approve as/ });
    expect(approves).toHaveLength(2);
    for (const b of approves) expect(b.className).not.toMatch(/primary/);
    expect(within(waiting).getAllByRole("button", { name: "Decline" })[0].className).toMatch(/secondary/);
  });
});
