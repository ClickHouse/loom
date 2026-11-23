import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AutomaticCaptureSuppressed } from "../../../sdk/typescript/src/session-protection.js";
import { post } from "../api";
import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { mcpConnection, mcpDiscovery, mcpPreviewValue, mcpProfile, mcpResult, mcpSource } from "../test/mcpSources";
import { MCPSourceSetup } from "./MCPSourceSetup";
import { AddMemoryDialog } from "./AddMemoryDialog";

// Only durable storage is replaced: controls, API methods and response observation are real.
vi.mock("../lib/dashboardSessionProtection", async () => {
  const { SessionProtection } = await import("../../../sdk/typescript/src/session-protection.js");
  const family = new SessionProtection({ read: async () => "protected", mark: async () => {} });
  return { dashboardSessionProtection: async () => family };
});

describe("source setup through the protected dashboard sender", () => {
  const sent: { op: string; body: Record<string, unknown>; declaration: string | null }[] = [];
  beforeEach(() => {
    sent.length = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit) => {
      const op = new URL(input).pathname.slice(4);
      const body = JSON.parse(String(init.body));
      sent.push({ op, body, declaration: new Headers(init.headers).get("x-loom-session-protection") });
      const responses: Record<string, unknown> = {
        "knowledge.source.mcp_profiles": { profiles: [mcpProfile], next_cursor: null, self_service_enabled: true },
        "knowledge.connection.mcp_list": { connections: [], next_cursor: null },
        "knowledge.connection.mcp_connect": { connection: mcpConnection },
        "knowledge.connection.mcp_inspect": { found: true, connection: mcpConnection, reconciliation: "present" },
        "knowledge.connection.mcp_discover": mcpDiscovery,
        "knowledge.source.mcp_preview": mcpPreviewValue,
        "knowledge.source.mcp_add": mcpResult,
        "collection.list": { collections: [{ namespace: "col-team", title: "Team", parent: "", personal: false, role: "admin" }], next_cursor: null },
        "knowledge.connection.mcp_browse": { slot: mcpConnection.slot, generation: mcpConnection.generation,
          mode: "resources", supported: true, items: [], complete: false, truncated: false,
          limitations: [], manual_resource_ids_supported: true, remote_identity_verified: false },
        "memory.set": { version: 1, pinned: false },
        "namespace.taxonomy": { tags: [], entities: [], communities: [] },
      };
      if (!(op in responses)) throw new Error(`Unexpected dispatch: ${op}`);
      return new Response(JSON.stringify({ ...(responses[op] as object), mirrored: true }), {
        headers: { "Content-Type": "application/json" },
      });
    }));
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("connects, discovers, reviews a private selection in a team collection and submits with human intent", async () => {
    const onSaved = vi.fn();
    render(<MCPSourceSetup org="acme" callerKey="owner" collectionVersion="grants" onSaved={onSaved} onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("radio", { name: "Approved remote server" }));
    await userEvent.type(screen.getByLabelText("Preissued upstream bearer"), "selected-secret");
    await userEvent.click(screen.getByRole("button", { name: "Connect a new account" }));
    await userEvent.click(await screen.findByRole("button", { name: "Discover capabilities" }));
    fireEvent.change(await screen.findByLabelText("Explicit resource IDs (one per line)"), { target: { value: mcpSource.resource_ids[0] } });
    fireEvent.click(screen.getByRole("radio", { name: "Selected snapshot" }));
    fireEvent.click(await screen.findByRole("radio", { name: "Team (col-team)" }));
    await userEvent.click(screen.getByRole("button", { name: "Review effective access" }));
    await screen.findByRole("region", { name: "Effective access preview" });
    await userEvent.click(screen.getByRole("button", { name: "Register source" }));
    await screen.findByText(/Server registration state: registered/);
    expect(onSaved).toHaveBeenCalledWith(mcpSource);
    expect(sent.filter((r) => r.op === "knowledge.source.mcp_add")).toEqual([{
      op: "knowledge.source.mcp_add", declaration: "v1",
      body: expect.objectContaining({ namespace: "col-team", reader_policy: "owner_only", resource_ids: mcpSource.resource_ids, confirm_copy_authority: false }),
    }]);
    expect(sent.every((r) => r.declaration === "v1")).toBe(true);
    expect(sent.filter((r) => r.op !== "knowledge.connection.mcp_connect").some((r) => JSON.stringify(r.body).includes("selected-secret"))).toBe(false);
    const count = sent.length;
    await expect(post("/v1/memory.set", { namespace: "other", session_id: "new", value: "private" })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(sent).toHaveLength(count);
  });

  it("dispatches an ordinary human memory submit and refuses the next automatic write in the same family", async () => {
    const onAdded = vi.fn();
    render(<AddMemoryDialog open identity={{ org: "acme", namespace: "col-team", agent: "loom-ui" }} onAdded={onAdded} onOpenChange={() => {}} />);
    await userEvent.type(screen.getByLabelText("Key (lower_snake_case)"), "manual_value");
    await userEvent.type(screen.getByLabelText("Value"), "human supplied value");
    await userEvent.click(screen.getByRole("button", { name: "Add memory" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalledOnce());
    expect(sent.filter((r) => r.op === "memory.set")).toEqual([{
      op: "memory.set", declaration: "v1", body: expect.objectContaining({ memory_key: "manual_value", value: "human supplied value" }),
    }]);
    const count = sent.length;
    await expect(post("/v1/memory.set", { namespace: "elsewhere", value: "private" })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(sent).toHaveLength(count);
  });
});
