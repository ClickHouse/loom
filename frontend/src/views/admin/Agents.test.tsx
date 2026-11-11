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
    expect(within(table).getByRole("status", { name: "Loading connected clients" })).toBeInTheDocument();
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
  it("marks a client not seen for a day with a Stale badge beside its time", async () => {
    const old = new Date(Date.now() - 3 * 86_400_000).toISOString();
    listConnectedAgents.mockResolvedValueOnce({ rows: [ROW, { ...ROW, agent: "cursor", last_seen: old }] });
    render(<Agents />);
    const stale = (await screen.findByText("cursor")).closest("tr") as HTMLElement;
    const fresh = screen.getByText("claude-code").closest("tr") as HTMLElement;
    expect(within(stale).getByText("Stale")).toBeInTheDocument();
    expect(within(stale).getByText("3d ago").tagName).toBe("TIME");
    expect(within(fresh).queryByText("Stale")).toBeNull();
  });
});

const toolbar = () => document.querySelector<HTMLElement>('[role="group"][aria-label="Time window and count"]');

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

// A read started by Refresh or Retry is dropped when a newer read overtakes it, so an answer
// for the window the page has left never lands after the new window's and draws stale rows.
describe("Agents: late answers", () => {
  it("an empty window switched for another reads as loading until the new window answers", async () => {
    listConnectedAgents.mockResolvedValueOnce({ rows: [] });
    render(<Agents />);
    expect(await screen.findByText("No client has written in this window")).toBeInTheDocument();
    let answer: (v: unknown) => void = () => {};
    listConnectedAgents.mockReturnValueOnce(new Promise((r) => (answer = r)));
    fireEvent.click(within(screen.getByRole("group", { name: "Time window" })).getByRole("button", { name: "7 days" }));
    await waitFor(() => expect(screen.queryByText("No client has written in this window")).toBeNull());
    expect(screen.queryByText("0 clients")).toBeNull();
    await act(async () => answer({ rows: [ROW] }));
    expect(await screen.findByText("claude-code")).toBeInTheDocument();
  });

  it("a switch away from a window with clients shows neither its rows nor its count", async () => {
    listConnectedAgents.mockResolvedValueOnce({ rows: [ROW] });
    render(<Agents />);
    await screen.findByText("claude-code");
    listConnectedAgents.mockReturnValueOnce(new Promise(() => {}));
    fireEvent.click(within(screen.getByRole("group", { name: "Time window" })).getByRole("button", { name: "7 days" }));
    await waitFor(() => expect(screen.queryByText("claude-code")).toBeNull());
    expect(screen.queryByText("1 client")).toBeNull();
  });

  it("Refresh on an empty window keeps its empty state on screen", async () => {
    listConnectedAgents.mockResolvedValueOnce({ rows: [] });
    render(<Agents />);
    expect(await screen.findByText("No client has written in this window")).toBeInTheDocument();
    // The empty window offers the connect in place, not a trip to Settings.
    expect(screen.getByRole("button", { name: "Connect an agent" })).toBeInTheDocument();
    expect(screen.queryByText(/Settings generates/)).toBeNull();
    listConnectedAgents.mockReturnValueOnce(new Promise(() => {}));
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    await waitFor(() => expect(listConnectedAgents).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("button", { name: /Refresh/ })).toHaveAttribute("aria-busy", "true"));
    expect(screen.getByText("No client has written in this window")).toBeInTheDocument();
  });

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
