import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type MCPBrowse, mcpBrowse } from "../api/mcpSources";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { MCPResourceBrowser } from "./MCPResourceBrowser";

vi.mock("../api/mcpSources", () => ({ mcpBrowse: vi.fn() }));
const window: MCPBrowse = {
  slot: "slot", generation: "generation", mode: "resources", supported: true,
  resources: true, mapped_read: false, mapped_search: false, capabilities_checked: true,
  items: [{ id: " opaque://A?x=%2F ", title: "Private title", uri: "mcp-resource-hint:opaque" }],
  complete: false, truncated: true, reason: "first_window_only", limitations: ["no_acl_or_permission_sync_proof"],
  manual_resource_ids_supported: true, remote_identity_verified: false,
};
const props = () => ({ org: "acme", slot: "slot", generation: "generation", contextKey: "authority-1", disabled: false, selected: [], onSelect: vi.fn() });
describe("bounded MCP metadata selection", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(mcpBrowse).mockResolvedValue(window); });
  afterEach(cleanup);
  it("requires a click, displays truncation and selects an exact ID without importing", async () => {
    const p = props(); render(<MCPResourceBrowser {...p} />);
    expect(mcpBrowse).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Read first metadata window" }));
    expect(mcpBrowse).toHaveBeenCalledWith({ org: "acme", slot: "slot", mode: "resources", query: "", limit: 100 }, expect.any(AbortSignal));
    expect(await screen.findByText(/Truncated: yes/)).toHaveTextContent("first_window_only");
    expect(screen.getByText(/Manual IDs remain possible/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /next|more|import/i })).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Select ID/ }));
    expect(p.onSelect).toHaveBeenCalledWith(window.items[0]?.id);
    expect(mcpBrowse).toHaveBeenCalledTimes(1);
  });
  it("shows unsupported mapped search as unchecked and retains manual entry guidance", async () => {
    vi.mocked(mcpBrowse).mockResolvedValue({ ...window, mode: "search", supported: false, resources: null, capabilities_checked: false, items: [], reason: "unsupported_mode" });
    render(<MCPResourceBrowser {...props()} />);
    fireEvent.click(screen.getByRole("radio", { name: "Mapped search" }));
    expect(screen.getByRole("button", { name: "Read first metadata window" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Mapped search query"), { target: { value: "private query" } });
    await userEvent.click(screen.getByRole("button", { name: "Read first metadata window" }));
    expect(await screen.findByText(/Resources: unchecked/)).toHaveTextContent("Capabilities checked: no");
    expect(screen.getByText(/Reason: unsupported_mode/)).toBeInTheDocument();
    expect(mcpBrowse).toHaveBeenCalledWith(expect.objectContaining({ mode: "search", query: "private query" }), expect.any(AbortSignal));
  });
  it("drops an abort-ignoring metadata answer after authority changes", async () => {
    let release!: (value: MCPBrowse) => void;
    const held = new Promise<MCPBrowse>((resolve) => { release = resolve; });
    vi.mocked(mcpBrowse).mockReturnValueOnce(held);
    const p = props(); const view = render(<MCPResourceBrowser {...p} />);
    await userEvent.click(screen.getByRole("button", { name: "Read first metadata window" }));
    await waitFor(() => expect(mcpBrowse).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(mcpBrowse).mock.calls[0]?.[1];
    view.rerender(<MCPResourceBrowser {...p} contextKey="authority-2" />);
    expect(signal?.aborted).toBe(true);
    await act(async () => { release(window); await held; });
    expect(screen.queryByText("Private title")).toBeNull();
    expect(p.onSelect).not.toHaveBeenCalled();
  });
  it("fails on generation drift or protected transport refusal without retry", async () => {
    vi.mocked(mcpBrowse).mockResolvedValueOnce({ ...window, generation: "other" });
    render(<MCPResourceBrowser {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: "Read first metadata window" }));
    await screen.findByRole("alert");
    expect(screen.queryByText("Private title")).toBeNull();
    vi.mocked(mcpBrowse).mockRejectedValueOnce(new Error("MCP_PROTECTED_SESSION_REQUIRED"));
    await userEvent.click(screen.getByRole("button", { name: "Read first metadata window" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Protected transport refusals remain failures");
    expect(mcpBrowse).toHaveBeenCalledTimes(2);
  });
});
