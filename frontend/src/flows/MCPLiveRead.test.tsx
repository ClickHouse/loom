import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type MCPLiveRead as LiveRead, mcpRead } from "../api/mcpSources";
import { mcpSource } from "../test/mcpSources";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { MCPLiveRead } from "./MCPLiveRead";

vi.mock("../api/mcpSources", () => ({ mcpRead: vi.fn() }));
const id = mcpSource.resource_ids[0] ?? "";
const read: LiveRead = { source_id: mcpSource.source_id, resource_id: id, uri: "mcp-resource:stable", title: "Private resource",
  text: "<script>private body</script>", mime_type: "text/plain", resource_identity_verified: false,
  remote_identity_verified: false, upstream_acl_verified: false, limitations: ["no_acl_or_permission_sync_proof"] };
const props = () => ({ org: "acme", source: { ...mcpSource, mode: "live" as const }, contextKey: "owner-authority", disabled: false });
async function requestRead() {
  fireEvent.click(screen.getByRole("radio", { name: id }));
  await userEvent.click(screen.getByRole("button", { name: "Read selected live resource" }));
}
describe("explicit saved live reads", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(mcpRead).mockResolvedValue(read); });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });
  it("loads one exact saved ID only on request, renders text and never stores or logs the body", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const push = vi.spyOn(history, "pushState");
    const replace = vi.spyOn(history, "replaceState");
    render(<MCPLiveRead {...props()} />);
    expect(mcpRead).not.toHaveBeenCalled();
    await requestRead();
    expect(await screen.findByLabelText("Live resource text")).toHaveTextContent(read.text);
    expect(screen.getByLabelText("Live resource text").querySelector("script")).toBeNull();
    expect(screen.getByText(/Stable citation:/)).toHaveTextContent(read.uri);
    expect(screen.getByText(/Resource identity verified: no/)).toHaveTextContent("Upstream ACL verified: no");
    expect(screen.getByText("no_acl_or_permission_sync_proof")).toBeInTheDocument();
    expect(mcpRead).toHaveBeenCalledWith({ org: "acme", namespace: mcpSource.namespace, name: mcpSource.name, resource_id: id }, expect.any(AbortSignal));
    for (const spy of [storage, log, error, info, warn, push, replace]) expect(spy).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Clear live content" }));
    expect(screen.queryByLabelText("Live resource text")).toBeNull();
    expect(mcpRead).toHaveBeenCalledTimes(1);
  });
  it("drops late bodies after clear or an authority change", async () => {
    let release!: (value: LiveRead) => void;
    const held = new Promise<LiveRead>((resolve) => { release = resolve; });
    vi.mocked(mcpRead).mockReturnValueOnce(held);
    const p = props(); const view = render(<MCPLiveRead {...p} />);
    await requestRead();
    await waitFor(() => expect(mcpRead).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(mcpRead).mock.calls[0]?.[1];
    await userEvent.click(screen.getByRole("button", { name: "Clear live content" }));
    view.rerender(<MCPLiveRead {...p} contextKey="revoked-authority" />);
    expect(signal?.aborted).toBe(true);
    await act(async () => { release(read); await held; });
    expect(screen.queryByLabelText("Live resource text")).toBeNull();
    expect(document.body.textContent).not.toContain(read.text);
  });
  it("clears displayed content when authority changes and never auto-refreshes it", async () => {
    const p = props(); const view = render(<MCPLiveRead {...p} />); await requestRead();
    await screen.findByLabelText("Live resource text");
    view.rerender(<MCPLiveRead {...p} contextKey="authority-2" />);
    expect(screen.queryByLabelText("Live resource text")).toBeNull();
    view.rerender(<MCPLiveRead {...p} />);
    expect(screen.queryByLabelText("Live resource text")).toBeNull();
    expect(mcpRead).toHaveBeenCalledTimes(1);
  });
  it("refuses mismatched source IDs and protected transport failures without retry or error-body display", async () => {
    vi.mocked(mcpRead).mockResolvedValueOnce({ ...read, source_id: "other" });
    render(<MCPLiveRead {...props()} />); await requestRead();
    await screen.findByRole("alert");
    expect(screen.queryByLabelText("Live resource text")).toBeNull();
    vi.mocked(mcpRead).mockRejectedValueOnce(new Error(`MCP_PROTECTED_SESSION_REQUIRED ${read.text}`));
    await userEvent.click(screen.getByRole("button", { name: "Read selected live resource" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("refusals remain failures");
    expect(document.body.textContent).not.toContain(read.text);
    expect(mcpRead).toHaveBeenCalledTimes(2);
  });
});
