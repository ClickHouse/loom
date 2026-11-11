// Health's Assistant row: who sees it for which readiness check, and the fix sentence
// `/v1/health` attaches while the assistant's setup fails.
import { act, cleanup, render, waitFor } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../../lib/state";
import { invalidateShared, sharedHealth } from "../../lib/sharedReads";
import { AssistantHealthRow } from "./AssistantHealthRow";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Check = "ready" | "not_configured" | "starting" | "failing" | "read_failed";
const FIX = "Set LOOM_ASSISTANT_LIBRECHAT_URL to a LibreChat Loom can reach.";

let check: Check = "ready";
let problem = false;

function readiness(): Response {
  const a =
    check === "ready"
      ? { ok: true, required: false }
      : check === "starting"
        ? { ok: false, reason: "starting", required: false }
        : { ok: false, reason: "failing", required: false };
  return json({ ready: check === "ready", checks: check === "not_configured" ? {} : { assistant: a } });
}

function signIn(platform: boolean) {
  useStore.setState({
    whoami: {
      authenticated: true,
      subject: "alice",
      org: "acme",
      namespaces: ["team"],
      roles: platform ? ["PLATFORM_ADMIN"] : ["VIEWER"],
    },
  });
}

async function shown(): Promise<HTMLElement | null> {
  const { container } = render(<AssistantHealthRow />);
  await waitFor(() => expect(container.querySelector("[data-focus-pending]")).toBeNull());
  return container.querySelector<HTMLElement>('[data-focus-key="health-assistant"]');
}

beforeEach(() => {
  invalidateShared();
  check = "ready";
  problem = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const path = url.split("?")[0];
      if (path === "/v1/health/ready") {
        if (check === "read_failed") throw new TypeError("Failed to fetch");
        return readiness();
      }
      if (path === "/v1/health")
        return json({
          status: "ok",
          problems: problem
            ? [{ code: "assistant_service_failing", severity: "error", message: "setup failed", fix: FIX }]
            : [],
        });
      return json({});
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("who sees the Assistant row", () => {
  it.each<[string, boolean, Check, boolean]>([
    ["a platform admin", true, "ready", true],
    ["a platform admin", true, "not_configured", true],
    ["a platform admin", true, "starting", true],
    ["a platform admin", true, "failing", true],
    ["a platform admin", true, "read_failed", false],
    ["a viewer", false, "ready", false],
    ["a viewer", false, "not_configured", false],
    ["a viewer", false, "starting", true],
    ["a viewer", false, "failing", true],
    ["a viewer", false, "read_failed", false],
  ])("%s (platform admin: %s), check %s: shown %s", async (_who, platform, c, visible) => {
    signIn(platform);
    check = c;
    expect(!!(await shown())).toBe(visible);
  });
});

describe("the fix sentence", () => {
  it.each<[Check, boolean]>([
    ["failing", true],
    ["starting", false],
    ["ready", false],
  ])("with the problem listed and check %s: shown %s", async (c, withFix) => {
    signIn(true);
    check = c;
    problem = true;
    const row = await shown();
    expect(row).not.toBeNull();
    if (withFix) await waitFor(() => expect(row).toHaveTextContent(FIX));
    else {
      // Once the health read has answered, the row still has no fix.
      await waitFor(() => expect(vi.mocked(fetch).mock.calls.map(([u]) => String(u))).toContain("/v1/health"));
      // The row's own handler on the shared read was registered first, so it has run.
      await act(async () => {
        await sharedHealth();
      });
      expect(row).not.toHaveTextContent(FIX);
    }
  });
});
