/** Refresh reads the clients again with the table still on screen: the table is marked
 *  busy and the button shows it is working, and nothing is swapped for a loader. */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../test/rtl";
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
  it("draws the first load as the table's header over a skeleton, marked busy", async () => {
    listConnectedAgents.mockReturnValue(new Promise(() => {}));
    render(<Agents />);
    const table = await screen.findByRole("table", { name: "Connected clients" });
    expect(table).toHaveAttribute("aria-busy", "true");
    // The first read waits 300 ms before its loader shows, like every loader
    // (ui/LoadingSkeleton), under the header row already in place.
    expect(within(table).queryByText("Loading data")).toBeNull();
    expect(within(table).getByRole("status", { name: "Loading connected clients" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
    expect(within(table).getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
  });

  it("keeps the table on screen, marked busy, while Refresh reads again", async () => {
    listConnectedAgents.mockResolvedValueOnce({ rows: [ROW] });
    render(<Agents />);
    expect(await screen.findByText("claude-code")).toBeInTheDocument();

    let answer: (v: unknown) => void = () => {};
    listConnectedAgents.mockReturnValueOnce(new Promise((r) => (answer = r)));
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    // The read starts from the effect, a microtask after the click.
    await waitFor(() => expect(screen.getByTestId("agents-table")).toHaveAttribute("aria-busy", "true"));
    expect(screen.getByText("claude-code")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Loading connected clients" })).toBeNull();

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

// Refresh and Retry abort the read in flight, so an answer for the window the page has left
// never lands after the new window's and draws stale rows.
describe("Agents: late answers", () => {
  it("drops a Refresh's answer that lands after a new window's", async () => {
    listConnectedAgents.mockResolvedValueOnce({ rows: [ROW] });
    render(<Agents />);
    await screen.findByText("claude-code");
    let late: (v: unknown) => void = () => {};
    listConnectedAgents.mockReturnValueOnce(new Promise((r) => (late = r)));
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    listConnectedAgents.mockResolvedValueOnce({ rows: [{ ...ROW, agent: "cursor" }] });
    fireEvent.click(within(screen.getByRole("group", { name: "Time window" })).getByRole("button", { name: "7 days" }));
    expect(await screen.findByText("cursor")).toBeInTheDocument();
    await act(async () => late({ rows: [{ ...ROW, agent: "stale-bot" }] }));
    expect(screen.queryByText("stale-bot")).toBeNull();
    expect(screen.getByText("cursor")).toBeInTheDocument();
  });
});
