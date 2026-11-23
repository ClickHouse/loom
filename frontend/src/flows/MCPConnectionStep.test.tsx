import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { mcpConnection, mcpDiscovery, mcpProfile } from "../test/mcpSources";
import type { MCPDiscovery } from "../api/mcpSources";
vi.mock("../api/mcpSources", async (original) => ({
  ...(await original<typeof import("../api/mcpSources")>()), mcpProfiles: vi.fn(), mcpConnections: vi.fn(),
  mcpConnect: vi.fn(), mcpInspectConnection: vi.fn(), mcpDiscover: vi.fn(), mcpDisconnect: vi.fn(),
}));
import * as api from "../api/mcpSources";
import { MCPConnectionStep } from "./MCPConnectionStep";

async function chooseProfile() { fireEvent.click(await screen.findByRole("radio", { name: "Approved remote server" })); }
async function chooseAccount() { await chooseProfile(); fireEvent.click(screen.getByRole("radio", { name: "My connection (configured)" })); }

describe("MCP connection setup", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.mcpProfiles).mockResolvedValue({ profiles: [mcpProfile], next_cursor: null, self_service_enabled: true });
    vi.mocked(api.mcpConnections).mockResolvedValue({ connections: [mcpConnection, { ...mcpConnection, slot: "second-slot", label: "Other connection", generation: "generation-b" }], next_cursor: null });
    vi.mocked(api.mcpInspectConnection).mockResolvedValue({ found: true, slot: mcpConnection.slot, connection: mcpConnection, reconciliation: "present" });
    vi.mocked(api.mcpDiscover).mockResolvedValue(mcpDiscovery);
    vi.mocked(api.mcpConnect).mockResolvedValue({ connection: mcpConnection });
    vi.mocked(api.mcpDisconnect).mockResolvedValue({ disconnected: true, upstream_revoked: false, upstream_action: "revoke_credential_upstream" });
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("discloses trusted origin/auth and clears credentials before connecting without storage, URL or log writes", async () => {
    render(<MCPConnectionStep org="acme" callerKey="owner" onChange={vi.fn()} />); await chooseProfile();
    expect(screen.getByText(/Endpoint origin: https:\/\/source.example/)).toBeInTheDocument();
    expect(screen.getByText(/Preissued bearer sent to this approved server/)).toBeInTheDocument();
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const history = vi.spyOn(window.history, "replaceState");
    const push = vi.spyOn(window.history, "pushState");
    const log = vi.spyOn(console, "log"); const warn = vi.spyOn(console, "warn"); const error = vi.spyOn(console, "error");
    const secret = "input-only-upstream-secret";
    await userEvent.type(screen.getByLabelText("Preissued upstream bearer"), secret);
    await userEvent.click(screen.getByRole("button", { name: "Connect a new account" }));
    await waitFor(() => expect(api.mcpConnect).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.mcpConnect).mock.calls[0]?.[0]).toEqual({ org: "acme", profile_id: mcpProfile.id, request_id: expect.any(String), bearer: secret });
    expect(screen.getByLabelText("Preissued upstream bearer")).toHaveValue("");
    expect(storage).not.toHaveBeenCalled(); expect(history).not.toHaveBeenCalled(); expect(push).not.toHaveBeenCalled();
    for (const spy of [log, warn, error]) expect(JSON.stringify(spy.mock.calls)).not.toContain(secret);
    expect(document.body.textContent).not.toContain(secret);
    expect(window.location.href).not.toContain(secret);
  });
  it("requires a disclosed HTTPS origin before credential entry or discovery", async () => {
    vi.mocked(api.mcpProfiles).mockResolvedValue({ profiles: [{ ...mcpProfile, endpoint_origin: "" }], next_cursor: null, self_service_enabled: true });
    render(<MCPConnectionStep org="acme" callerKey="owner" onChange={vi.fn()} />); await chooseAccount();
    expect(screen.queryByLabelText("Preissued upstream bearer")).toBeNull();
    expect(screen.getByRole("button", { name: "Connect a new account" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Discover capabilities" })).toBeDisabled();
    expect(api.mcpConnect).not.toHaveBeenCalled(); expect(api.mcpDiscover).not.toHaveBeenCalled();
  });
  it("supports anonymous profiles without a bearer", async () => {
    vi.mocked(api.mcpProfiles).mockResolvedValue({ profiles: [{ ...mcpProfile, auth: "none" }], next_cursor: null, self_service_enabled: true });
    render(<MCPConnectionStep org="acme" callerKey="owner" onChange={vi.fn()} />); await chooseProfile();
    await userEvent.click(screen.getByRole("button", { name: "Connect a new account" }));
    expect(vi.mocked(api.mcpConnect).mock.calls[0]?.[0]).not.toHaveProperty("bearer");
  });
  it("reconciles an uncertain connect by its original nonce without resubmitting credentials", async () => {
    vi.mocked(api.mcpConnect).mockRejectedValue(new Error("cut response"));
    render(<MCPConnectionStep org="acme" callerKey="owner" onChange={vi.fn()} />); await chooseProfile();
    await userEvent.type(screen.getByLabelText("Preissued upstream bearer"), "secret");
    await userEvent.click(screen.getByRole("button", { name: "Connect a new account" }));
    await screen.findByRole("button", { name: "Inspect connection result" });
    const nonce = vi.mocked(api.mcpConnect).mock.calls[0]?.[0].request_id;
    await waitFor(() => expect(screen.getByRole("button", { name: "Inspect connection result" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Inspect connection result" }));
    expect(api.mcpInspectConnection).toHaveBeenCalledWith("acme", { request_id: nonce }, expect.any(AbortSignal));
    expect(api.mcpConnect).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Preissued upstream bearer")).toHaveValue("");
  });
  it("drops a late discovery after the selected connection changes", async () => {
    let release!: (result: MCPDiscovery) => void;
    const held = new Promise<MCPDiscovery>((resolve) => { release = resolve; });
    vi.mocked(api.mcpDiscover).mockReturnValueOnce(held);
    const changed = vi.fn(); render(<MCPConnectionStep org="acme" callerKey="owner" onChange={changed} />); await chooseAccount();
    await userEvent.click(screen.getByRole("button", { name: "Discover capabilities" }));
    // Simulate a native focus refresh, which remains possible during the read.
    fireEvent(window, new Event("focus"));
    await act(async () => { release(mcpDiscovery); await held; });
    expect(changed.mock.calls.some(([value]) => value?.discovery)).toBe(false);
  });
  it("requires separate disconnect confirmation and reports no upstream revocation", async () => {
    const changed = vi.fn(); render(<MCPConnectionStep org="acme" callerKey="owner" onChange={changed} />); await chooseAccount();
    expect(screen.getByRole("button", { name: "Disconnect connection" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /Disconnect this connection and stop all registrations/ }));
    await userEvent.click(screen.getByRole("button", { name: "Disconnect connection" }));
    expect(api.mcpDisconnect).toHaveBeenCalledWith("acme", mcpConnection.slot, expect.any(AbortSignal), "explicit");
    expect(await screen.findByText(/Upstream credential revoked: no/)).toHaveTextContent("Revoke it with the upstream service yourself");
    expect(changed).toHaveBeenLastCalledWith(null);
  });
  it("retains the exact uncertain disconnect target through auth-context invalidation", async () => {
    vi.mocked(api.mcpDisconnect).mockRejectedValue(new Error("cut disconnect response"));
    const changed = vi.fn();
    const view = render(<MCPConnectionStep org="acme" callerKey="owner" contextKey="auth-1" onChange={changed} />); await chooseAccount();
    fireEvent.click(screen.getByRole("checkbox", { name: /Disconnect this connection and stop all registrations/ }));
    await userEvent.click(screen.getByRole("button", { name: "Disconnect connection" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Inspect connection result" })).toBeEnabled());
    view.rerender(<MCPConnectionStep org="acme" callerKey="owner" contextKey="auth-2" onChange={changed} />);
    await userEvent.click(screen.getByRole("button", { name: "Inspect connection result" }));
    expect(api.mcpInspectConnection).toHaveBeenLastCalledWith("acme", { slot: mcpConnection.slot }, expect.any(AbortSignal));
    expect(api.mcpDisconnect).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/The mutation result is uncertain/)).toBeInTheDocument();
  });
  it("rejects generation drift before running capability discovery", async () => {
    vi.mocked(api.mcpInspectConnection).mockResolvedValue({ found: true, slot: mcpConnection.slot, connection: { ...mcpConnection, generation: "new-generation" }, reconciliation: "present" });
    const changed = vi.fn(); render(<MCPConnectionStep org="acme" callerKey="owner" onChange={changed} />); await chooseAccount();
    await userEvent.click(screen.getByRole("button", { name: "Discover capabilities" }));
    await screen.findByRole("alert"); expect(api.mcpDiscover).not.toHaveBeenCalled();
    expect(changed.mock.calls.some(([value]) => value?.discovery)).toBe(false);
  });
});
