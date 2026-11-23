import { describe, expect, it, vi } from "vitest";
import {
  AutomaticCaptureSuppressed,
  SessionProtection,
  type SessionProtectionStore,
  SessionProtectionUnavailable,
} from "../../../sdk/typescript/src/session-protection.js";
import { createDashboardTransport } from "./dashboardTransport";

function control() {
  let state: "clean" | "protected" | "missing" = "clean";
  const store: SessionProtectionStore = {
    async read() {
      if (state === "missing") throw new Error("missing proof");
      return state;
    },
    async mark() {
      if (state === "missing") throw new Error("missing proof");
      state = "protected";
    },
  };
  return {
    family: new SessionProtection(store),
    store,
    lose: () => { state = "missing"; },
    state: () => state,
  };
}

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json", "X-Request-Id": "retained-request" },
  });
}

const post: RequestInit = { method: "POST", body: "{}" };
const baseUrl = "https://dashboard.example";

describe("dashboard transport", () => {
  it("observes a manual private read before blocking all later automatic destinations", async () => {
    const state = control();
    const family = vi.fn(async () => state.family);
    const fetch = vi.fn(async (_url: string, _init: RequestInit) => json({ mirrored: true, text: "private" }));
    const send = createDashboardTransport({ baseUrl, family, fetch });
    const response = await send("/v1/knowledge.source.mcp_read", post, "explicit");
    expect(state.state()).toBe("protected");
    expect(await response.json()).toEqual({ mirrored: true, text: "private" });
    expect(new Headers(fetch.mock.calls[0]?.[1]?.headers).get("x-loom-session-protection")).toBe("v1");
    await expect(send("/v1/memory.set", {
      ...post, body: JSON.stringify({ namespace: "another", session_id: "new-request", value: "private" }),
    })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    await expect(send("/v1/conversation.create", post)).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(family).toHaveBeenCalledTimes(1);
  });

  it("allows a clean automatic call without manufacturing a declaration in application code", async () => {
    const state = control();
    const fetch = vi.fn(async (_url: string, _init: RequestInit) => json({ ok: true }));
    const send = createDashboardTransport({ baseUrl, family: async () => state.family, fetch });
    await expect((await send("/v1/memory.set", post)).json()).resolves.toEqual({ ok: true });
    expect(new Headers(fetch.mock.calls[0]?.[1]?.headers).get("x-loom-session-protection")).toBe("v1");
    expect(state.state()).toBe("clean");
  });

  it("keeps manual intent on one invocation and observes further explicit writes", async () => {
    const state = control();
    await state.family.mark();
    const fetch = vi.fn(async () => json({ mirrored: true, saved: true }));
    const send = createDashboardTransport({ baseUrl, family: async () => state.family, fetch });
    expect((await send("/v1/memory.set", post, "explicit")).status).toBe(200);
    await expect(send("/v1/memory.set", post)).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("observes error metadata before returning status, headers or diagnostics", async () => {
    const state = control();
    const fetch = vi.fn(async () => json({ error: { data: { mirrored: true } } }, 500));
    const send = createDashboardTransport({ baseUrl, family: async () => state.family, fetch });
    const response = await send("/v1/knowledge.source.mcp_read", post, "explicit");
    expect(state.state()).toBe("protected");
    expect(response.status).toBe(500);
    expect(response.headers.get("X-Request-Id")).toBe("retained-request");
    await expect(send("/v1/memory.set", post)).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
  });

  it.each([200, 403, 503])("refuses malformed private JSON at HTTP %i", async (status) => {
    const state = control();
    const fetch = vi.fn(async () => new Response('{"mirrored":true,', {
      status, headers: { "Content-Type": "application/json" },
    }));
    const send = createDashboardTransport({ baseUrl, family: async () => state.family, fetch });
    await expect(send("/v1/knowledge.source.mcp_read", post, "explicit")).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(state.state()).toBe("protected");
    await expect(send("/v1/memory.set", post)).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
  });

  it("rejects missing proof for manual calls and does not replace the retained family", async () => {
    const state = control();
    const family = vi.fn(async () => state.family);
    const fetch = vi.fn(async () => json({}));
    const send = createDashboardTransport({ baseUrl, family, fetch });
    await send("/v1/memory.get", post, "explicit");
    state.lose();
    await expect(send("/v1/memory.get", post, "explicit")).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    await expect(send("/v1/memory.set", post)).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(family).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("does not retry a failed application restore", async () => {
    const family = vi.fn(async (): Promise<SessionProtection> => { throw new SessionProtectionUnavailable(); });
    const fetch = vi.fn(async () => json({}));
    const send = createDashboardTransport({ baseUrl, family, fetch });
    await expect(send("/v1/memory.set", post, "explicit")).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    await expect(send("/v1/memory.set", post)).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(family).toHaveBeenCalledTimes(1);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("observes SSE before a private frame is released", async () => {
    const state = control();
    const frame = 'data: {"mirrored":true,"delta":"private"}\n\n';
    const fetch = vi.fn(async () => new Response(frame, { headers: { "Content-Type": "text/event-stream" } }));
    const send = createDashboardTransport({ baseUrl, family: async () => state.family, fetch });
    const response = await send("/v1/assistant.chat", post, "explicit");
    expect(await response.text()).toBe(frame);
    expect(state.state()).toBe("protected");
    await expect(send("/v1/conversation.create", post)).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
  });

  it("joins stream cancellation and refuses automatic work before unread content can escape", async () => {
    const state = control();
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ cancel }, { highWaterMark: 0 });
    const fetch = vi.fn(async () => new Response(body, { headers: { "Content-Type": "text/event-stream" } }));
    const send = createDashboardTransport({ baseUrl, family: async () => state.family, fetch });
    const response = await send("/v1/assistant.chat", post, "explicit");
    await response.body?.cancel();
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(state.state()).toBe("protected");
    await expect(send("/v1/memory.set", post)).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
  });

  it("refuses incomplete SSE EOF and retains the same family for later requests", async () => {
    const state = control();
    const fetch = vi.fn(async () => new Response('data: {"mirrored":true}', {
      headers: { "Content-Type": "text/event-stream" },
    }));
    const send = createDashboardTransport({ baseUrl, family: async () => state.family, fetch });
    const response = await send("/v1/assistant.chat", post, "explicit");
    await expect(response.text()).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(state.state()).toBe("protected");
    await expect(send("/v1/memory.set", post)).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("marks the retained family when the caller aborts an outstanding JSON request", async () => {
    const state = control();
    let started: () => void = () => {};
    const opening = new Promise<void>((resolve) => { started = resolve; });
    const fetch = vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      started();
    }));
    const send = createDashboardTransport({ baseUrl, family: async () => state.family, fetch });
    const controller = new AbortController();
    const pending = send("/v1/knowledge.source.mcp_read", { ...post, signal: controller.signal }, "explicit");
    await opening;
    controller.abort();
    await expect(pending).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(state.state()).toBe("protected");
    await expect(send("/v1/memory.set", post)).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("waits for durable marking before releasing content or admitting another automatic callback", async () => {
    let state: "clean" | "protected" = "clean";
    let marking: () => void = () => {};
    const started = new Promise<void>((resolve) => { marking = resolve; });
    let commit: () => void = () => {};
    const committed = new Promise<void>((resolve) => { commit = resolve; });
    const family = new SessionProtection({
      read: async () => state,
      mark: async () => { marking(); await committed; state = "protected"; },
    });
    const fetch = vi.fn(async () => json({ mirrored: true, text: "private" }));
    const send = createDashboardTransport({ baseUrl, family: async () => family, fetch });
    let released = false;
    const pending = send("/v1/knowledge.source.mcp_read", post, "explicit").then((response) => {
      released = true;
      return response;
    });
    await started;
    expect(released).toBe(false);
    expect(family.locallyProtected).toBe(true);
    const automatic = send("/v1/memory.set", post);
    const refused = expect(automatic).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    commit();
    await expect((await pending).json()).resolves.toEqual({ mirrored: true, text: "private" });
    await refused;
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("rejects foreign origins and caller declarations before sending credentials", async () => {
    const state = control();
    const fetch = vi.fn(async () => json({}));
    const send = createDashboardTransport({ baseUrl, family: async () => state.family, fetch });
    await expect(send("https://other.example/v1/memory.set", {
      ...post, headers: { Authorization: "Bearer example" },
    }, "explicit")).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    await expect(send("/v1/memory.set", {
      ...post, headers: { "x-loom-session-protection": "v1" },
    }, "explicit")).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(fetch).not.toHaveBeenCalled();
  });
});
