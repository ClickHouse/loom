/** Refresh reads the clients again with the table still on screen: the table is marked
 *  busy and the button shows it is working, and nothing is swapped for a loader. */
import { act, cleanup, fireEvent, render, screen, within } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../../lib/state";
import { Agents } from "./Agents";
import { ApiError } from "../../lib/errors";

const listConnectedAgents = vi.fn();
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listConnectedAgents: (...a: unknown[]) => listConnectedAgents(...a) };
});

const ROW = {
  agent: "claude-code",
  ops: 3,
  namespaces_touched: 1,
  primary_namespace: "team",
  cost_usd: 0,
  last_seen: new Date().toISOString(),
};

beforeEach(() => {
  listConnectedAgents.mockReset();
  useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" } });
});
afterEach(() => {
  cleanup();
  useStore.setState({ identity: null });
});

describe("Agents: loading", () => {
  it("draws the first load as Click UI's loading table, marked busy", async () => {
    listConnectedAgents.mockReturnValue(new Promise(() => {}));
    render(<Agents />);
    const table = await screen.findByRole("table", { name: "Connected clients" });
    expect(table).toHaveAttribute("aria-busy", "true");
    expect(within(table).getByText("Loading data")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
  });

  it("keeps the table on screen, marked busy, while Refresh reads again", async () => {
    listConnectedAgents.mockResolvedValueOnce({ rows: [ROW] });
    render(<Agents />);
    expect(await screen.findByText("claude-code")).toBeInTheDocument();

    let answer: (v: unknown) => void = () => {};
    listConnectedAgents.mockReturnValueOnce(new Promise((r) => (answer = r)));
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    expect(screen.getByText("claude-code")).toBeInTheDocument();
    expect(screen.getByTestId("agents-table")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("Loading data")).toBeNull();

    await act(async () => answer({ rows: [ROW] }));
    expect(screen.getByTestId("agents-table")).not.toHaveAttribute("aria-busy");
  });
});

describe("Agents: last seen", () => {
  it("marks a client not seen for a day with a Stale badge, its time in muted text", async () => {
    const old = new Date(Date.now() - 3 * 86_400_000).toISOString();
    listConnectedAgents.mockResolvedValueOnce({ rows: [ROW, { ...ROW, agent: "cursor", last_seen: old }] });
    render(<Agents />);
    const stale = (await screen.findByText("cursor")).closest("tr") as HTMLElement;
    const fresh = screen.getByText("claude-code").closest("tr") as HTMLElement;
    const badge = within(stale).getByText("Stale");
    expect(badge.closest('[data-testid="ellipsed-badge-content"]')).not.toBeNull();
    const time = within(stale).getByText("3d ago");
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveClass("text-text-muted");
    expect(stale.querySelector(".text-warn")).toBeNull();
    expect(within(fresh).queryByText("Stale")).toBeNull();
  });
});

const toolbar = () => document.querySelector<HTMLElement>('search[aria-label="Time window and count"]');

describe("Agents: count and failure", () => {
  it("counts the clients at the end of the toolbar", async () => {
    listConnectedAgents.mockResolvedValueOnce({ rows: [ROW, { ...ROW, agent: "cursor" }] });
    render(<Agents />);
    await screen.findByText("cursor");
    expect(toolbar()).toHaveTextContent("2 clients");
  });

  it("says a failed read failed, with its request id and Retry, never the empty state", async () => {
    listConnectedAgents.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "UNAVAILABLE", message: "down", requestId: "req-9" }),
    );
    render(<Agents />);
    expect(await screen.findByText("Couldn't load connected clients")).toBeInTheDocument();
    expect(screen.getByText(/req-9/)).toBeInTheDocument();
    expect(screen.queryByText("No client has written in this window")).toBeNull();
    expect(toolbar()).not.toHaveTextContent(/client/);
    listConnectedAgents.mockResolvedValueOnce({ rows: [ROW] });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("claude-code")).toBeInTheDocument();
  });
});
