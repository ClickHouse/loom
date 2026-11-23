import { describe, expect, it, vi } from "vitest";
import {
  AutomaticCaptureSuppressed,
  SessionProtection,
} from "../../../sdk/typescript/src/session-protection.js";
import { createDashboardTransport } from "./dashboardTransport";
import { OP_BY_NAME } from "../../../sdk/typescript/src/generated/ops.js";
import { ADMIN_SETTINGS_READ_ROUTES } from "../api/adminReadRoutes";

function sender() {
  const family = new SessionProtection({
    read: async () => "protected",
    mark: async () => {},
  });
  const fetch = vi.fn(async (_url: string, _init: RequestInit) => new Response('{"mirrored":true}', {
    headers: { "Content-Type": "application/json" },
  }));
  return { fetch, send: createDashboardTransport({ baseUrl: "https://dashboard.example", family: async () => family, fetch }) };
}

describe("source read dispatch classification", () => {
  it.each(Object.values(ADMIN_SETTINGS_READ_ROUTES))(
    "observes the dedicated operator read %s without granting a mutation", async (path) => {
      const { fetch, send } = sender();
      await send(path, { method: "POST", body: "{}" });
      expect(fetch).toHaveBeenCalledOnce();
      await expect(send("/v1/admin/settings.set", { method: "POST", body: "{}" })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
      expect(fetch).toHaveBeenCalledOnce();
    },
  );
  it.each(["/v1/admin/settings.list?prefix=secret", "/v1/admin/settings.list/", "/v1/admin/settings.history#fragment", "/v1/admin/settings.reset"])(
    "keeps the unregistered operator variant %s guarded", async (path) => {
      const { fetch, send } = sender();
      await expect(send(path, { method: "POST", body: "{}" })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
      expect(fetch).not.toHaveBeenCalled();
    },
  );
  it.each(Object.entries(OP_BY_NAME).filter(([, spec]) => spec.readOnly).map(([name]) => name))(
    "admits the generated read-only contract for %s", async (op) => {
      const { fetch, send } = sender();
      await send(`/v1/${op}`, { method: "POST", body: "{}" });
      expect(fetch).toHaveBeenCalledOnce();
    },
  );

  it.each(["/v1/constructor", "/v1/__proto__", "/v1/memory.list?limit=1", "/v1/memory.list/", "https://dashboard.example/v1/memory.list"])(
    "keeps noncanonical or unknown POST %s guarded", async (path) => {
      const { fetch, send } = sender();
      await expect(send(path, { method: "POST", body: "{}" })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
      expect(fetch).not.toHaveBeenCalled();
    },
  );
  it.each(["memory.list", "memory.get", "conversation.list", "conversation.search", "namespace.inventory"])(
    "admits the trusted generated read-only operation %s", async (op) => {
      expect(OP_BY_NAME[op as keyof typeof OP_BY_NAME]?.readOnly).toBe(true);
      const { fetch, send } = sender();
      await send(`/v1/${op}`, { method: "POST", body: "{}" });
      expect(fetch).toHaveBeenCalledOnce();
    },
  );
  it.each([
    "source.mcp_read", "connection.mcp_browse", "source.mcp_preview", "source.mcp_list",
    "source.mcp_inspect", "source.mcp_profiles", "connection.mcp_list", "connection.mcp_inspect",
  ])("observes %s without treating it as automatic publication", async (op) => {
    const { fetch, send } = sender();
    const path = `/v1/knowledge.${op}`;
    const response = await send(path, { method: "POST", body: "{}" });
    expect(await response.json()).toEqual({ mirrored: true });
    expect(new Headers(fetch.mock.calls[0]?.[1]?.headers).get("x-loom-session-protection")).toBe("v1");
    await expect(send("/v1/memory.set", { method: "POST", body: "{}" })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    "source.mcp_add", "source.mcp_update", "source.mcp_remove", "source.mcp_refresh",
    "source.mcp_pause", "source.mcp_resume", "connection.mcp_connect", "connection.mcp_disconnect",
    "connection.mcp_discover", "source.mcp_read?write=true", "source.mcp_read/extra",
    "source.mcp_read#fragment", "source.mcp_read%3Fwrite", "unknown.get", "constructor",
  ])("keeps %s guarded without inferring manual intent", async (op) => {
    const { fetch, send } = sender();
    await expect(send(`/v1/knowledge.${op}`, { method: "POST", body: "{}" })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(fetch).not.toHaveBeenCalled();
  });
});
