import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { axeViolations } from "../test/axe";
import { mcpResult, mcpSource, mcpSummary as summary } from "../test/mcpSources";
import type { MCPSourceSummary } from "../api/mcpSources";
vi.mock("../api/mcpSources", async (original) => ({
  ...(await original<typeof import("../api/mcpSources")>()), mcpList: vi.fn(), mcpInspect: vi.fn(),
  mcpControl: vi.fn(), mcpRemove: vi.fn(),
}));
vi.mock("../api/knowledgeSources", async (original) => ({
  ...(await original<typeof import("../api/knowledgeSources")>()), listKnowledgeSources: vi.fn(),
}));
import * as api from "../api/mcpSources";
import { listKnowledgeSources } from "../api/knowledgeSources";
import { useStore } from "../lib/state";
import { MCPSources } from "./MCPSources";
import { SourcesList } from "./SourcesList";

const initialStore = useStore.getState();
async function openDetails() { await userEvent.click(await screen.findByRole("button", { name: "Private documents", exact: true })); await screen.findByRole("dialog"); }
async function action(label: string) {
  await userEvent.click(screen.getByRole("button", { name: `Actions for ${mcpSource.name}` }));
  await userEvent.click(await screen.findByRole("menuitem", { name: label, exact: true }));
}
function owned() {
  useStore.setState({ authed: true, sessionExpired: false,
    identity: { org: "acme", namespace: "col-team", agent: "dashboard" },
    whoami: { org: "acme", subject: "owner-a", roles: ["ADMIN"], authenticated: true }, myNamespaces: [],
  });
}

describe("owner MCP source management", () => {
  beforeEach(() => {
    vi.clearAllMocks(); owned();
    vi.mocked(api.mcpList).mockResolvedValue({ sources: [summary], next_cursor: null });
    vi.mocked(api.mcpInspect).mockResolvedValue({ found: true, source: mcpSource });
    vi.mocked(api.mcpControl).mockResolvedValue({ ...mcpResult, source: { ...mcpSource, state: "paused" } });
    vi.mocked(api.mcpRemove).mockResolvedValue({ ...mcpResult, source: { ...mcpSource, state: "retired" } });
    vi.mocked(listKnowledgeSources).mockRejectedValue(new Error("native source operation unavailable"));
  });
  afterEach(() => { cleanup(); useStore.setState(initialStore); vi.restoreAllMocks(); });

  it("integrates into the real Sources entry point even when the replaced adapter read fails", async () => {
    const panel = document.createElement("div"); document.body.append(panel);
    const view = render(<SourcesList org="acme" namespace="col-team" place={{ label: "Team", noun: "collection" }} manageable section="Sources" panel={panel} />);
    await userEvent.click(await screen.findByRole("button", { name: "MCP sources", exact: true }));
    expect(await screen.findByRole("button", { name: "Set up MCP source" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Private documents", exact: true })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add a source", exact: true })).toBeNull();
    view.unmount(); panel.remove();
  });
  it("pages owner-only summaries without displaying resource IDs until exact inspection", async () => {
    vi.mocked(api.mcpInspect).mockResolvedValue({ found: true, source: { ...mcpSource, name: "second" } });
    vi.mocked(api.mcpList).mockResolvedValueOnce({ sources: [summary], next_cursor: "next-page" }).mockResolvedValueOnce({ sources: [{ ...summary, name: "second", label: "Second registration" }], next_cursor: null });
    render(<MCPSources org="acme" namespace="col-team" />);
    await screen.findByRole("button", { name: "Private documents", exact: true });
    expect(screen.queryByText(mcpSource.resource_ids[0] ?? "missing")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Load more registrations" }));
    expect(await screen.findByRole("button", { name: "Second registration", exact: true })).toBeInTheDocument();
    expect(api.mcpList).toHaveBeenLastCalledWith("acme", "col-team", "next-page", expect.any(AbortSignal));
    expect(screen.getByRole("button", { name: "Previous registrations" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Second registration", exact: true }));
    expect(api.mcpInspect).toHaveBeenCalledWith({ org: "acme", namespace: "col-team", name: "second" }, expect.any(AbortSignal));
    expect(await screen.findByText(mcpSource.resource_ids[0] ?? "missing")).toBeInTheDocument();
  });
  it("requires exact removal confirmation, retains copies by default, and reports server disposition", async () => {
    render(<MCPSources org="acme" namespace="col-team" />); await openDetails(); await action("Remove registration");
    expect(screen.getByRole("button", { name: "Confirm removal" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Also request erasure of stored copies" })).not.toBeChecked();
    await userEvent.type(screen.getByLabelText("Repeat the exact registration name to remove"), mcpSource.name);
    await userEvent.click(screen.getByRole("button", { name: "Confirm removal" }));
    expect(api.mcpRemove).toHaveBeenCalledWith({ org: "acme", namespace: "col-team", name: mcpSource.name, confirm: mcpSource.name, delete_documents: false }, expect.any(AbortSignal), "explicit");
    expect(await screen.findByText(/Copies: retained/)).toBeInTheDocument();
  });
  it("requires separate explicit erasure confirmation", async () => {
    vi.mocked(api.mcpRemove).mockResolvedValue({ ...mcpResult, copies: "erasure_requested", source: { ...mcpSource, state: "retired", pending_erasure: true } });
    render(<MCPSources org="acme" namespace="col-team" />); await openDetails(); await action("Remove registration");
    await userEvent.type(screen.getByLabelText("Repeat the exact registration name to remove"), mcpSource.name);
    fireEvent.click(screen.getByRole("checkbox", { name: "Also request erasure of stored copies" }));
    expect(screen.getByRole("button", { name: "Confirm removal" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Type ERASE to confirm asynchronous copy erasure"), "ERASE");
    await userEvent.click(screen.getByRole("button", { name: "Confirm removal" }));
    expect(api.mcpRemove).toHaveBeenCalledWith(expect.objectContaining({ delete_documents: true }), expect.any(AbortSignal), "explicit");
    expect(await screen.findByText(/Copies: erasure_requested/)).toBeInTheDocument();
  });
  it("pauses, resumes and explicitly refreshes nonlive registrations using server truth", async () => {
    render(<MCPSources org="acme" namespace="col-team" />); await openDetails(); await action("Pause");
    expect(api.mcpControl).toHaveBeenLastCalledWith(expect.objectContaining({ name: mcpSource.name }), "pause", expect.any(AbortSignal), "explicit");
    await screen.findByText(/Server state: paused/);
    vi.mocked(api.mcpControl).mockResolvedValue({ ...mcpResult, source: mcpSource });
    await action("Resume"); await screen.findByText(/Server state: registered/);
    vi.mocked(api.mcpControl).mockResolvedValue({ ...mcpResult, refresh_requested: true });
    await action("Refresh selected items");
    expect(api.mcpControl).toHaveBeenLastCalledWith(expect.any(Object), "refresh", expect.any(AbortSignal), "explicit");
    expect(await screen.findByText(/Refresh requested: yes/)).toBeInTheDocument();
  });
  it("disables explicit refresh in live mode", async () => {
    vi.mocked(api.mcpInspect).mockResolvedValue({ found: true, source: { ...mcpSource, mode: "live" } });
    render(<MCPSources org="acme" namespace="col-team" />); await openDetails();
    await userEvent.click(screen.getByRole("button", { name: `Actions for ${mcpSource.name}` }));
    expect(screen.getByRole("menuitem", { name: "Refresh selected items" })).toHaveAttribute("aria-disabled", "true");
    expect(api.mcpControl).not.toHaveBeenCalled();
  });
  it("reconciles uncertain mutations without issuing them again", async () => {
    vi.mocked(api.mcpControl).mockRejectedValue(new Error("cut response"));
    render(<MCPSources org="acme" namespace="col-team" />); await openDetails(); await action("Pause");
    await screen.findByText(/The mutation result is uncertain/);
    await userEvent.click(screen.getByRole("button", { name: "Inspect current source state" }));
    await screen.findByText(/Current server state inspected/);
    expect(api.mcpControl).toHaveBeenCalledTimes(1);
    expect(api.mcpInspect).toHaveBeenCalledTimes(2);
  });
  it("does not show another caller's late owner list", async () => {
    let release!: (value: { sources: MCPSourceSummary[]; next_cursor: null }) => void;
    const held = new Promise<{ sources: MCPSourceSummary[]; next_cursor: null }>((resolve) => { release = resolve; });
    vi.mocked(api.mcpList).mockReturnValueOnce(held).mockResolvedValue({ sources: [], next_cursor: null });
    render(<MCPSources org="acme" namespace="col-team" />);
    await waitFor(() => expect(api.mcpList).toHaveBeenCalledTimes(1));
    act(() => useStore.setState({ whoami: { org: "acme", subject: "owner-b", roles: ["ADMIN"], authenticated: true } }));
    await screen.findByText(/No owned MCP registrations/);
    await act(async () => { release({ sources: [summary], next_cursor: null }); await held; });
    expect(screen.queryByRole("button", { name: "Private documents", exact: true })).toBeNull();
  });
  it("removes old owner metadata on session revocation and makes no new source call", async () => {
    render(<MCPSources org="acme" namespace="col-team" />); await openDetails();
    act(() => useStore.setState({ sessionExpired: true }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(mcpSource.resource_ids[0] ?? "missing")).toBeNull();
    expect(screen.getByText(/Sign in with your own user account/)).toBeInTheDocument();
    expect(api.mcpList).toHaveBeenCalledTimes(1);
  });
  it("shows the writer requirement after destination grant loss", async () => {
    render(<MCPSources org="acme" namespace="col-team" />); await openDetails();
    act(() => useStore.setState({ whoami: { org: "acme", subject: "owner-a", roles: ["VIEWER"], authenticated: true }, myNamespaces: [] }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText(/Needs writer on col-team/)).toBeInTheDocument();
  });
  it("has no axe violations on the owner list and management dialog", async () => {
    const view = render(<MCPSources org="acme" namespace="col-team" />);
    await screen.findByRole("button", { name: "Private documents", exact: true });
    expect(await axeViolations(view.container)).toEqual([]);
    await openDetails(); expect(await axeViolations(screen.getByRole("dialog"))).toEqual([]);
  });
});
