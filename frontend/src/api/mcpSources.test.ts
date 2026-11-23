import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("../api", () => ({ post: vi.fn() }));
import { post } from "../api";
import {
  mcpAdd, mcpConnect, mcpConnections, mcpControl, mcpDiscover, mcpDisconnect, mcpInspect,
  mcpInspectConnection, mcpList, mcpPreview, mcpProfiles, mcpRemove, mcpUpdate, mcpBrowse, mcpRead,
} from "./mcpSources";

const send = vi.mocked(post);
const selected = { org: "acme", namespace: "col-team", slot: "slot", resource_ids: ["opaque://A?x=%2F", " ID with spaces "], mode: "snapshot", reader_policy: "owner_only" } as const;

describe("MCP source operation boundary", () => {
  beforeEach(() => { vi.clearAllMocks(); send.mockResolvedValue({}); });
  it("uses the shared transport and sends only the connection contract, never invented authority", async () => {
    const signal = new AbortController().signal;
    const input = { org: "acme", profile_id: "approved", request_id: "nonce", bearer: "secret", owner: "other", endpoint: "https://other.example", tool: "hidden", headers: { protection: "pretend" } };
    await mcpConnect(input, signal);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith("/v1/knowledge.connection.mcp_connect", {
      org: "acme", profile_id: "approved", request_id: "nonce", bearer: "secret",
    }, signal, "automatic");
  });
  it("does not retry a protected-client or session refusal", async () => {
    const failure = new Error("SESSION_PROTECTION_REQUIRED");
    send.mockRejectedValue(failure);
    await expect(mcpConnect({ org: "acme", profile_id: "p", request_id: "n" })).rejects.toBe(failure);
    expect(send).toHaveBeenCalledTimes(1);
  });
  it("preserves opaque IDs and has no hidden endpoint, owner, tool or scope fields", async () => {
    const input = { ...selected, resource_ids: [...selected.resource_ids], owner: "other", scope: "all", endpoint: "https://other.example" };
    await mcpPreview(input);
    expect(send).toHaveBeenLastCalledWith("/v1/knowledge.source.mcp_preview", { ...selected, resource_ids: [...selected.resource_ids] }, undefined, "automatic");
    await mcpAdd({ ...input, request_id: "same-nonce", label: null, confirm_copy_authority: false });
    expect(send).toHaveBeenLastCalledWith("/v1/knowledge.source.mcp_add", {
      ...selected, resource_ids: [...selected.resource_ids], request_id: "same-nonce", label: null, confirm_copy_authority: false,
    }, undefined, "automatic");
  });
  it("reconciles additions with the original UUID and slot and strips extra authority", async () => {
    const request_id = "4a8dbf51-c184-4e4d-8e45-83139cdfc1ba";
    const input = { org: "acme", namespace: "col-team", slot: "original-slot", request_id, owner: "other", endpoint: "https://other.example", source_id: "override" };
    await mcpInspect(input);
    expect(send).toHaveBeenLastCalledWith("/v1/knowledge.source.mcp_inspect", { org: "acme", namespace: "col-team", slot: "original-slot", request_id }, undefined, "automatic");
  });
  it("browses one bounded metadata window with no cursor or arbitrary tool", async () => {
    const input = { org: "acme", slot: "slot", mode: "search" as const, query: "private query", limit: 37, cursor: "hidden", tool: "execute", endpoint: "https://other.example", owner: "other" };
    await mcpBrowse(input);
    expect(send).toHaveBeenLastCalledWith("/v1/knowledge.connection.mcp_browse", { org: "acme", slot: "slot", mode: "search", query: "private query", limit: 37 }, undefined, "automatic");
    await mcpBrowse({ ...input, mode: "resources" });
    expect(send).toHaveBeenLastCalledWith("/v1/knowledge.connection.mcp_browse", { org: "acme", slot: "slot", mode: "resources", query: "", limit: 37 }, undefined, "automatic");
  });
  it("reads only a saved source and exact resource ID without any authority overrides", async () => {
    const input = { org: "acme", namespace: "col-team", name: "opaque-registration", resource_id: " opaque://A?x=%2F ", endpoint: "https://other.example", tool: "hidden", slot: "override", source_id: "override", owner: "other", headers: { protection: "pretend" } };
    await mcpRead(input);
    expect(send).toHaveBeenLastCalledWith("/v1/knowledge.source.mcp_read", { org: input.org, namespace: input.namespace, name: input.name, resource_id: input.resource_id }, undefined, "automatic");
    send.mockRejectedValueOnce(new Error("MCP_PROTECTED_SESSION_REQUIRED"));
    await expect(mcpRead(input)).rejects.toThrow("MCP_PROTECTED_SESSION_REQUIRED");
    expect(send).toHaveBeenCalledTimes(2);
  });
  it("exposes every management operation, bounds owner-list pages and omits mutable audience on update", async () => {
    const signal = new AbortController().signal;
    const key = { org: "acme", namespace: "col-team", name: "registration" };
    await mcpProfiles("acme", "profiles-next", signal);
    await mcpConnections("acme", "accounts-next", signal);
    await mcpInspectConnection("acme", { request_id: "connect-nonce" }, signal);
    await mcpDiscover("acme", "slot", signal);
    await mcpDisconnect("acme", "slot", signal);
    await mcpList("acme", "col-team", "next", signal);
    await mcpInspect(key, signal);
    await mcpUpdate({ ...key, if_scope_revision: 4, resource_ids: ["opaque"], mode: "live", label: "new", confirm_copy_authority: false, reader_policy: "collection_copy" } as Parameters<typeof mcpUpdate>[0], signal);
    await mcpControl(key, "pause", signal); await mcpControl(key, "resume", signal); await mcpControl(key, "refresh", signal);
    await mcpRemove({ ...key, confirm: "registration", delete_documents: false }, signal);
    expect(send.mock.calls.map(([path]) => path)).toEqual([
      "/v1/knowledge.source.mcp_profiles", "/v1/knowledge.connection.mcp_list", "/v1/knowledge.connection.mcp_inspect",
      "/v1/knowledge.connection.mcp_discover", "/v1/knowledge.connection.mcp_disconnect", "/v1/knowledge.source.mcp_list",
      "/v1/knowledge.source.mcp_inspect", "/v1/knowledge.source.mcp_update", "/v1/knowledge.source.mcp_pause",
      "/v1/knowledge.source.mcp_resume", "/v1/knowledge.source.mcp_refresh", "/v1/knowledge.source.mcp_remove",
    ]);
    expect(send.mock.calls[5]?.[1]).toEqual({ org: "acme", namespace: "col-team", cursor: "next", limit: 20, include_retired: true });
    expect(send.mock.calls[7]?.[1]).toEqual({ ...key, if_scope_revision: 4, resource_ids: ["opaque"], mode: "live", label: "new", confirm_copy_authority: false });
    for (const call of send.mock.calls) expect(call[2]).toBe(signal);
    for (const call of send.mock.calls) expect(call[3]).toBe("automatic");
  });
});
