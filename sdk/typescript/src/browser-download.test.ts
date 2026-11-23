import { describe, expect, it, vi } from "vitest";
import { protectedBrowserTransport } from "./browser-protection.js";
import { AutomaticCaptureSuppressed, SessionProtection, SessionProtectionUnavailable } from "./session-protection.js";

function fixture() {
  const state: { value: "clean" | "protected" } = { value: "clean" };
  const root = new SessionProtection({
    read: async () => state.value,
    mark: async () => { state.value = "protected"; },
  });
  return { state, root };
}

describe("observed opaque GET downloads", () => {
  it.each(["application/octet-stream", "text/plain", "application/pdf", "application/x-ndjson", "application/json"])(
    "marks before releasing %s content without inferring an envelope", async (media) => {
      const { state, root } = fixture();
      const fetch = vi.fn(async () => new Response("opaque\ncontent", { headers: { "Content-Type": media } }));
      const transport = protectedBrowserTransport({ baseUrl: "https://loom.invalid", sessionProtection: root, fetch });
      const response = await transport.downloadFetch("/download", { method: "GET" });
      expect(state.value).toBe("protected");
      expect(await response.text()).toBe("opaque\ncontent");
      expect(fetch).toHaveBeenCalledOnce();
      await expect(transport.automaticFetch("/v1/memory.set", { method: "POST" })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
      expect(fetch).toHaveBeenCalledOnce();
    },
  );

  it("holds the response until durable marking is acknowledged", async () => {
    let release!: () => void;
    let marking!: () => void;
    const ack = new Promise<void>((resolve) => { release = resolve; });
    const entered = new Promise<void>((resolve) => { marking = resolve; });
    let state: "clean" | "protected" = "clean";
    const root = new SessionProtection({
      read: async () => state,
      mark: async () => { marking(); await ack; state = "protected"; },
    });
    const transport = protectedBrowserTransport({
      baseUrl: "https://loom.invalid", sessionProtection: root,
      fetch: async () => new Response("private bytes"),
    });
    let returned = false;
    const pending = transport.downloadFetch("/download", { method: "GET" });
    void pending.then(() => { returned = true; }, () => { returned = true; });
    await entered;
    expect(returned).toBe(false);
    release();
    expect(await (await pending).text()).toBe("private bytes");
    expect(state).toBe("protected");
  });

  it("refuses failed durable marking before returning an opaque body", async () => {
    const root = new SessionProtection({ read: async () => "clean", mark: async () => { throw new Error("lost proof"); } });
    const transport = protectedBrowserTransport({
      baseUrl: "https://loom.invalid", sessionProtection: root,
      fetch: async () => new Response("unreleased bytes"),
    });
    await expect(transport.downloadFetch("/download", { method: "GET" })).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(root.locallyProtected).toBe(true);
  });

  it("enforces the application's tighter byte bound and retains protection", async () => {
    const { state, root } = fixture();
    const transport = protectedBrowserTransport({
      baseUrl: "https://loom.invalid", sessionProtection: root, maxDownloadBytes: 8,
      fetch: async () => new Response("123456789"),
    });
    await expect(transport.downloadFetch("/download", { method: "GET" })).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(state.value).toBe("protected");
  });

  it("refuses POST downloads and foreign-origin credential dispatch", async () => {
    const { root } = fixture();
    const fetch = vi.fn(async () => new Response("must not be fetched"));
    const transport = protectedBrowserTransport({ baseUrl: "https://loom.invalid", sessionProtection: root, fetch });
    await expect(transport.downloadFetch("/download", { method: "POST" })).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    await expect(transport.downloadFetch("https://other.invalid/download", { method: "GET" })).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(fetch).not.toHaveBeenCalled();
  });
});

it("joins a cancelled opaque reader and keeps its family protected", async () => {
  const { state, root } = fixture();
  let calls = 0;
  let cancelled = false;
  let stalled!: () => void;
  const waiting = new Promise<void>((resolve) => { stalled = resolve; });
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (++calls === 1) controller.enqueue(new TextEncoder().encode("prefix"));
      else stalled();
    },
    cancel() { cancelled = true; },
  });
  const signal = new AbortController();
  const transport = protectedBrowserTransport({
    baseUrl: "https://loom.invalid", sessionProtection: root,
    fetch: async () => new Response(body),
  });
  const pending = transport.downloadFetch("/download", { method: "GET", signal: signal.signal });
  await waiting;
  signal.abort();
  await expect(pending).rejects.toBeInstanceOf(SessionProtectionUnavailable);
  expect(cancelled).toBe(true);
  expect(state.value).toBe("protected");
});

it("bounds opaque chunk bookkeeping independently from response bytes", async () => {
  const { state, root } = fixture();
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) { controller.enqueue(new Uint8Array([1])); },
    cancel() { cancelled = true; },
  });
  const transport = protectedBrowserTransport({
    baseUrl: "https://loom.invalid", sessionProtection: root,
    fetch: async () => new Response(body),
  });
  await expect(transport.downloadFetch("/download", { method: "GET" })).rejects.toBeInstanceOf(SessionProtectionUnavailable);
  expect(cancelled).toBe(true);
  expect(state.value).toBe("protected");
});
