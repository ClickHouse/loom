import { afterEach, expect, it, vi } from "vitest";
import { dashboardFetch } from "./dashboardTransport";
import { flushView, startView } from "./vitals";

vi.mock("./dashboardSessionProtection", async () => {
  const { SessionProtection } = await import("../../../sdk/typescript/src/session-protection.js");
  const family = new SessionProtection({ read: async () => "protected", mark: async () => {} });
  return { dashboardSessionProtection: async () => family };
});

afterEach(() => vi.unstubAllGlobals());

it("guards automatic telemetry with the retained protected family", async () => {
  const fetch = vi.fn(async () => new Response("{}", { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetch);
  startView("knowledge");
  flushView({ org: "one", namespace: "one" }, "build");
  // Joining another automatic invocation uses the same opening family and guard.
  await expect(dashboardFetch("/v1/ui.vitals", { method: "POST", body: "{}" }))
    .rejects.toMatchObject({ name: "AutomaticCaptureSuppressed" });
  expect(fetch).not.toHaveBeenCalled();
});
