/** Refresh reads the clients again with the table still on screen: the table is marked
 *  busy and the button shows it is working, and nothing is swapped for a loader. */
import { act, cleanup, fireEvent, render, screen, within } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../../lib/state";
import { Agents } from "./Agents";

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
