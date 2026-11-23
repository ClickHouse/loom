import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { axeViolations } from "../test/axe";
import { mcpConnection, mcpDiscovery, mcpPreviewValue, mcpProfile, mcpResult, mcpSource } from "../test/mcpSources";
import type { MCPPreview } from "../api/mcpSources";

vi.mock("../api/mcpSources", async (original) => ({
  ...(await original<typeof import("../api/mcpSources")>()),
  mcpProfiles: vi.fn(), mcpConnections: vi.fn(), mcpConnect: vi.fn(), mcpInspectConnection: vi.fn(),
  mcpDiscover: vi.fn(), mcpDisconnect: vi.fn(), mcpPreview: vi.fn(), mcpAdd: vi.fn(), mcpInspect: vi.fn(), mcpUpdate: vi.fn(),
}));
vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()), listCollections: vi.fn(), createCollection: vi.fn(),
}));
import * as api from "../api/mcpSources";
import { createCollection, listCollections } from "../api";
import { MCPSourceSetup, resourceIDs } from "./MCPSourceSetup";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const props = () => ({ org: "acme", callerKey: "owner:auth-1", collectionVersion: "grants-1", onSaved: vi.fn(), onClose: vi.fn() });
async function chooseConnection() {
  fireEvent.click(await screen.findByRole("radio", { name: "Approved remote server" }));
  fireEvent.click(await screen.findByRole("radio", { name: "My connection (configured)" }));
  await userEvent.click(screen.getByRole("button", { name: "Discover capabilities" }));
  await screen.findByLabelText("Explicit resource IDs (one per line)");
}
async function selection() {
  await chooseConnection();
  fireEvent.change(screen.getByLabelText("Explicit resource IDs (one per line)"), { target: { value: mcpSource.resource_ids[0] } });
  fireEvent.click(screen.getByRole("radio", { name: "Selected snapshot" }));
  fireEvent.click(await screen.findByRole("radio", { name: "Team (col-team)" }));
}
async function review() {
  await userEvent.click(screen.getByRole("button", { name: "Review effective access" }));
  await screen.findByRole("region", { name: "Effective access preview" });
}

describe("generic MCP source setup", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.mcpProfiles).mockResolvedValue({ profiles: [mcpProfile], next_cursor: null, self_service_enabled: true });
    vi.mocked(api.mcpConnections).mockResolvedValue({ connections: [mcpConnection], next_cursor: null });
    vi.mocked(api.mcpInspectConnection).mockResolvedValue({ found: true, slot: mcpConnection.slot, connection: mcpConnection, reconciliation: "present" });
    vi.mocked(api.mcpDiscover).mockResolvedValue(mcpDiscovery);
    vi.mocked(api.mcpConnect).mockResolvedValue({ connection: mcpConnection });
    vi.mocked(api.mcpPreview).mockResolvedValue(mcpPreviewValue);
    vi.mocked(api.mcpAdd).mockResolvedValue(mcpResult);
    vi.mocked(api.mcpInspect).mockResolvedValue({ found: true, source: mcpSource });
    vi.mocked(api.mcpUpdate).mockResolvedValue(mcpResult);
    vi.mocked(listCollections).mockResolvedValue({ collections: [{ namespace: "col-team", title: "Team", parent: "", personal: false, role: "admin" }], next_cursor: null });
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("selects an existing owner connection, discovers, chooses opaque content/mode/destination, previews and saves", async () => {
    const p = props(); render(<MCPSourceSetup {...p} />);
    expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
    await selection();
    expect(screen.getByText(/complete enumeration are not supported/)).toBeInTheDocument();
    await review();
    expect(screen.getByText(/Server access rule: owner_only/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Register source" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Register source" }));
    expect(api.mcpAdd).toHaveBeenCalledTimes(1);
    expect(api.mcpAdd).toHaveBeenCalledWith(expect.objectContaining({
      org: "acme", namespace: "col-team", slot: mcpConnection.slot, resource_ids: mcpSource.resource_ids,
      mode: "snapshot", reader_policy: "owner_only", confirm_copy_authority: false, request_id: expect.any(String),
    }), expect.any(AbortSignal), "explicit");
    expect(p.onSaved).toHaveBeenCalledWith(mcpSource);
    expect(await screen.findByText(/Server registration state: registered/)).toHaveTextContent("does not attest upstream access");
  });
  it("connects a new account and requires discovery before the complete source flow", async () => {
    vi.mocked(api.mcpConnections).mockResolvedValue({ connections: [], next_cursor: null });
    render(<MCPSourceSetup {...props()} />);
    fireEvent.click(await screen.findByRole("radio", { name: "Approved remote server" }));
    await userEvent.type(screen.getByLabelText("Preissued upstream bearer"), "input-only-secret");
    await userEvent.click(screen.getByRole("button", { name: "Connect a new account" }));
    expect(screen.queryByLabelText("Explicit resource IDs (one per line)")).toBeNull();
    await userEvent.click(await screen.findByRole("button", { name: "Discover capabilities" }));
    fireEvent.change(await screen.findByLabelText("Explicit resource IDs (one per line)"), { target: { value: mcpSource.resource_ids[0] } });
    fireEvent.click(screen.getByRole("radio", { name: "Selected snapshot" }));
    fireEvent.click(await screen.findByRole("radio", { name: "Team (col-team)" })); await review();
    await userEvent.click(screen.getByRole("button", { name: "Register source" }));
    expect(api.mcpConnect).toHaveBeenCalledTimes(1); expect(api.mcpAdd).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(vi.mocked(api.mcpAdd).mock.calls)).not.toContain("input-only-secret");
  });
  it("requires explicit publication consent and rechecks it after content or grant changes", async () => {
    vi.mocked(api.mcpPreview).mockResolvedValue({ ...mcpPreviewValue, rule: { ...mcpPreviewValue.rule, access: "collection_wide", publication_confirmation_required: true, collection_readers_receive_copy: true } });
    const p = props(); const view = render(<MCPSourceSetup {...p} />);
    await selection(); fireEvent.click(screen.getByRole("radio", { name: "Copy to collection readers" }));
    fireEvent.click(await screen.findByRole("radio", { name: "Team (col-team)" })); await review();
    expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /I have authority to publish/ }));
    expect(screen.getByRole("button", { name: "Register source" })).toBeEnabled();
    view.rerender(<MCPSourceSetup {...p} collectionVersion="grants-2" />);
    expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
    expect(screen.queryByRole("region", { name: "Effective access preview" })).toBeNull();
  });
  it("sends publication authority only after intentional collection copy consent", async () => {
    vi.mocked(api.mcpPreview).mockResolvedValue({ ...mcpPreviewValue, rule: { ...mcpPreviewValue.rule, access: "collection_wide", publication_confirmation_required: true } });
    render(<MCPSourceSetup {...props()} />); await selection();
    fireEvent.click(screen.getByRole("radio", { name: "Copy to collection readers" }));
    fireEvent.click(await screen.findByRole("radio", { name: "Team (col-team)" })); await review();
    fireEvent.click(screen.getByRole("checkbox", { name: /I have authority to publish/ }));
    await userEvent.click(screen.getByRole("button", { name: "Register source" }));
    expect(api.mcpAdd).toHaveBeenCalledWith(expect.objectContaining({ reader_policy: "collection_copy", confirm_copy_authority: true }), expect.any(AbortSignal), "explicit");
  });
  it("drops an abort-ignoring late preview after selection changes", async () => {
    const held = deferred<MCPPreview>(); vi.mocked(api.mcpPreview).mockReturnValueOnce(held.promise);
    render(<MCPSourceSetup {...props()} />); await selection();
    await userEvent.click(screen.getByRole("button", { name: "Review effective access" }));
    // A collection/grant invalidation remains possible while a request is in flight.
    fireEvent(window, new Event("focus"));
    await act(async () => { held.resolve(mcpPreviewValue); await held.promise; });
    expect(screen.queryByRole("region", { name: "Effective access preview" })).toBeNull();
    expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
    expect(api.mcpAdd).not.toHaveBeenCalled();
  });
  it("aborts a pending preview when grants change and rejects its late answer", async () => {
    const held = deferred<MCPPreview>(); vi.mocked(api.mcpPreview).mockReturnValueOnce(held.promise);
    const p = props(); const view = render(<MCPSourceSetup {...p} />); await selection();
    await userEvent.click(screen.getByRole("button", { name: "Review effective access" }));
    await waitFor(() => expect(api.mcpPreview).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(api.mcpPreview).mock.calls[0]?.[1];
    view.rerender(<MCPSourceSetup {...p} collectionVersion="revoked-grants" />);
    expect(signal?.aborted).toBe(true);
    await act(async () => { held.resolve(mcpPreviewValue); await held.promise; });
    expect(screen.queryByRole("region", { name: "Effective access preview" })).toBeNull();
    expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
  });
  it("drops caller-owned state and late discovery when the authenticated caller changes", async () => {
    const held = deferred<typeof mcpDiscovery>(); vi.mocked(api.mcpDiscover).mockReturnValueOnce(held.promise);
    const p = props(); const view = render(<MCPSourceSetup {...p} />);
    fireEvent.click(await screen.findByRole("radio", { name: "Approved remote server" }));
    fireEvent.click(screen.getByRole("radio", { name: "My connection (configured)" }));
    await userEvent.type(screen.getByLabelText("Preissued upstream bearer"), "old-caller-secret");
    await userEvent.click(screen.getByRole("button", { name: "Discover capabilities" }));
    await waitFor(() => expect(api.mcpDiscover).toHaveBeenCalledTimes(1));
    view.rerender(<MCPSourceSetup {...p} callerKey="different-caller" />);
    await act(async () => { held.resolve(mcpDiscovery); await held.promise; });
    expect(screen.queryByLabelText("Explicit resource IDs (one per line)")).toBeNull();
    expect(screen.queryByDisplayValue("old-caller-secret")).toBeNull();
    expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
  });
  it("invalidates preview synchronously on mode/label changes and never accepts unavailable access", async () => {
    render(<MCPSourceSetup {...props()} />); await selection(); await review();
    fireEvent.change(screen.getByLabelText("Source label (optional)"), { target: { value: "new label" } });
    expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
    vi.mocked(api.mcpPreview).mockResolvedValue({ ...mcpPreviewValue, rule: { ...mcpPreviewValue.rule, access: "unavailable", reason: "operator_refused" } });
    await review(); expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: "Live lookup" }));
    expect(screen.queryByRole("region", { name: "Effective access preview" })).toBeNull();
  });
  it("keeps the draft, nonce and confirmed collection after uncertain add and never resends", async () => {
    vi.mocked(api.mcpAdd).mockRejectedValue(new Error("unreadable result"));
    vi.mocked(createCollection).mockResolvedValue({ namespace: "col-new", parent: "", created: true });
    vi.mocked(listCollections).mockResolvedValue({ collections: [
      { namespace: "col-team", title: "Team", parent: "", personal: false, role: "admin" },
      { namespace: "col-new", title: "New library", parent: "", personal: false, role: "admin" },
    ], next_cursor: null });
    render(<MCPSourceSetup {...props()} />); await selection();
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    await userEvent.type(screen.getByLabelText("Title"), "New library");
    await userEvent.click(screen.getByRole("button", { name: "Create", exact: true }));
    await screen.findByText(/Confirmed collections retained: col-new/);
    fireEvent.click(await screen.findByRole("radio", { name: "Team (col-team)" })); await review();
    await userEvent.click(screen.getByRole("button", { name: "Register source" }));
    await screen.findByText(/The mutation result is uncertain/);
    const nonce = vi.mocked(api.mcpAdd).mock.calls[0]?.[0].request_id;
    expect(screen.getByText(/Retained request ID:/)).toHaveTextContent(nonce ?? "missing");
    expect(screen.getByLabelText("Explicit resource IDs (one per line)")).toHaveValue(mcpSource.resource_ids[0]);
    expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
    fireEvent(window, new Event("focus"));
    expect(screen.getByText(/Retained request ID:/)).toHaveTextContent(nonce ?? "missing");
    await userEvent.click(screen.getByRole("button", { name: "Inspect source result" }));
    expect(api.mcpInspect).toHaveBeenCalledWith({ org: "acme", namespace: "col-team", slot: mcpConnection.slot, request_id: nonce }, expect.any(AbortSignal));
    expect(nonce).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    expect(screen.queryByLabelText("Registration name returned by the server")).toBeNull();
    expect(api.mcpAdd).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Confirmed collections retained: col-new/)).toBeInTheDocument();
  });
  it("edits using the inspected scope revision without an audience or connection mutation", async () => {
    render(<MCPSourceSetup {...props()} source={mcpSource} />);
    fireEvent.change(screen.getByLabelText("Explicit resource IDs (one per line)"), { target: { value: "replacement" } });
    await review(); await userEvent.click(screen.getByRole("button", { name: "Save source changes" }));
    expect(api.mcpUpdate).toHaveBeenCalledWith({ org: "acme", namespace: "col-team", name: mcpSource.name,
      if_scope_revision: 4, resource_ids: ["replacement"], mode: "snapshot", label: "Private documents", confirm_copy_authority: false }, expect.any(AbortSignal), "explicit");
    expect(screen.queryByRole("radiogroup", { name: "Publication audience" })).toBeNull();
  });
  it("accepts only the server-reconciled original binding after an unreadable add", async () => {
    vi.mocked(api.mcpAdd).mockRejectedValue(new Error("unreadable result"));
    const p = props(); render(<MCPSourceSetup {...p} />); await selection(); await review();
    await userEvent.click(screen.getByRole("button", { name: "Register source" }));
    await userEvent.click(await screen.findByRole("button", { name: "Inspect source result" }));
    const submitted = vi.mocked(api.mcpAdd).mock.calls[0]?.[0];
    expect(api.mcpInspect).toHaveBeenCalledWith({ org: "acme", namespace: "col-team", slot: submitted?.slot, request_id: submitted?.request_id }, expect.any(AbortSignal));
    expect(await screen.findByText(/server reconciled the original request/)).toBeInTheDocument();
    expect(p.onSaved).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Accept inspected server state" }));
    expect(p.onSaved).toHaveBeenCalledWith(mcpSource);
    expect(api.mcpAdd).toHaveBeenCalledTimes(1);
  });
  it.each([
    { found: false, source: null },
    { found: true, source: { ...mcpSource, namespace: "other-destination" } },
    { found: true, source: { ...mcpSource, slot: "other-slot" } },
  ])("keeps absent or mismatched reconciliation blocked without a guessed name or retry: %j", async (answer) => {
    vi.mocked(api.mcpAdd).mockRejectedValue(new Error("unreadable result"));
    vi.mocked(api.mcpInspect).mockResolvedValue(answer);
    render(<MCPSourceSetup {...props()} />); await selection(); await review();
    await userEvent.click(screen.getByRole("button", { name: "Register source" }));
    await userEvent.click(await screen.findByRole("button", { name: "Inspect source result" }));
    await screen.findByText(/No matching owned registration/);
    expect(screen.queryByRole("button", { name: "Accept inspected server state" })).toBeNull();
    expect(screen.getByRole("button", { name: "Register source" })).toBeDisabled();
    expect(api.mcpAdd).toHaveBeenCalledTimes(1);
  });
  it("drops late reconciliation after caller changes while retaining the original request in the sent read", async () => {
    vi.mocked(api.mcpAdd).mockRejectedValue(new Error("unreadable result"));
    const held = deferred<Awaited<ReturnType<typeof api.mcpInspect>>>();
    vi.mocked(api.mcpInspect).mockReturnValueOnce(held.promise);
    const p = props(); const view = render(<MCPSourceSetup {...p} />); await selection(); await review();
    await userEvent.click(screen.getByRole("button", { name: "Register source" }));
    await userEvent.click(await screen.findByRole("button", { name: "Inspect source result" }));
    await waitFor(() => expect(api.mcpInspect).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(api.mcpInspect).mock.calls[0]?.[1];
    view.rerender(<MCPSourceSetup {...p} callerKey="different-owner" />);
    expect(signal?.aborted).toBe(true);
    await act(async () => { held.resolve({ found: true, source: mcpSource }); await held.promise; });
    expect(screen.queryByRole("button", { name: "Accept inspected server state" })).toBeNull();
    expect(screen.queryByText(/Retained request ID:/)).toBeNull();
    expect(p.onSaved).not.toHaveBeenCalled();
    expect(api.mcpAdd).toHaveBeenCalledTimes(1);
  });
  it("refuses an edit if the server revision changed", async () => {
    vi.mocked(api.mcpInspect).mockResolvedValue({ found: true, source: { ...mcpSource, scope_revision: 5 } });
    render(<MCPSourceSetup {...props()} source={mcpSource} />); await review();
    await userEvent.click(screen.getByRole("button", { name: "Save source changes" }));
    await screen.findByText(/The mutation result is uncertain/);
    expect(api.mcpUpdate).not.toHaveBeenCalled();
  });
  it("has named accessible controls and no axe violations", async () => {
    render(<MCPSourceSetup {...props()} />); await selection(); await review();
    expect(await axeViolations(screen.getByRole("dialog"))).toEqual([]);
  });
  it("splits only on newline and preserves opaque identity bytes", () => {
    expect(resourceIDs(" opaque://A?x=%2F \nhttps://source.example/a//b?x=1#part")).toEqual([" opaque://A?x=%2F ", "https://source.example/a//b?x=1#part"]);
    expect(resourceIDs("")).toEqual([]);
  });
});
