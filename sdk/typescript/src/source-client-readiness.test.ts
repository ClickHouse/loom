import { describe, expect, it } from "vitest";
import { protectedBrowserTransport } from "./browser-protection.js";
import {
  AutomaticCaptureSuppressed,
  SessionCapture,
  SessionProtection,
  SessionProtectionUnavailable,
} from "./session-protection.js";
import { Transport } from "./transport.js";

const declaration = "x-loom-session-protection";
const privateValue = "private-source-value";

function family() {
  let state: "clean" | "protected" = "clean";
  let missing = false;
  const root = new SessionProtection({
    async read() {
      if (missing) throw new Error(privateValue);
      return state;
    },
    async mark() {
      if (missing) throw new Error(privateValue);
      state = "protected";
    },
  });
  return {
    root,
    lose: () => {
      missing = true;
    },
    state: () => state,
  };
}

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json", "X-Request-Id": "protected" },
  });
}

describe("actual SDK request readiness", () => {
  it("declares only with a bound family and blocks cross-namespace automatic capture after observation", async () => {
    const source = family();
    const requests: RequestInit[] = [];
    const client = new Transport({
      baseUrl: "https://loom.invalid",
      apiKey: "secret",
      sessionProtection: source.root,
      logLevel: "off",
      fetch: async (_url, init) => {
        requests.push(init);
        return json({ mirrored: true, value: privateValue });
      },
    });
    expect(await client.post("knowledge.source.mcp_read", {})).toEqual({ mirrored: true, value: privateValue });
    expect(new Headers(requests[0]?.headers).get(declaration)).toBe("v1");
    expect(new Headers(requests[0]?.headers).get("authorization")).toBe("Bearer secret");
    expect(source.state()).toBe("protected");
    await expect(
      client.post(
        "memory.set",
        { namespace: "other", value: privateValue },
        { capture: new SessionCapture([source.root]) },
      ),
    ).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(requests).toHaveLength(1);
    await client.post("memory.get", { org: "other", session_id: "other" });
    expect(requests).toHaveLength(2);
  });

  it("does not change the promise of ordinary sessions", async () => {
    const client = new Transport({
      baseUrl: "https://loom.invalid",
      logLevel: "off",
      fetch: async (_url, init) => {
        expect(new Headers(init.headers).has(declaration)).toBe(false);
        return json({ ok: true });
      },
    });
    expect(await client.post("memory.get", {})).toEqual({ ok: true });
  });

  it("preserves the server readiness refusal for an unbound source read", async () => {
    const client = new Transport({
      baseUrl: "https://loom.invalid",
      logLevel: "off",
      fetch: async (url, init) => {
        expect(new URL(url).pathname).toBe("/v1/knowledge.source.mcp_read");
        expect(new Headers(init.headers).has(declaration)).toBe(false);
        return json({ detail: { code: "SESSION_PROTECTION_REQUIRED", message: "Session protection required" } }, 409);
      },
    });
    await expect(client.post("knowledge.source.mcp_read", {})).rejects.toMatchObject({
      code: "SESSION_PROTECTION_REQUIRED",
    });
  });

  it.each([
    { [declaration]: "v1" },
    { [declaration]: "v2" },
    { [declaration]: "v1", "X-Loom-Session-Protection": "v1" },
  ])("refuses caller declarations %j", async (headers) => {
    let sent = 0;
    for (const bound of [false, true]) {
      const client = new Transport({
        baseUrl: "https://loom.invalid",
        headers,
        sessionProtection: bound ? family().root : undefined,
        logLevel: "off",
        fetch: async () => {
          sent++;
          return json({});
        },
      });
      await expect(client.post("knowledge.source.mcp_read", {})).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    }
    expect(sent).toBe(0);
  });

  it("refuses lost state without exposing store diagnostics", async () => {
    const source = family();
    source.lose();
    let sent = 0;
    const client = new Transport({
      baseUrl: "https://loom.invalid",
      sessionProtection: source.root,
      logLevel: "off",
      fetch: async () => {
        sent++;
        return json({});
      },
    });
    await expect(client.post("knowledge.source.mcp_read", {})).rejects.toThrow(
      "Session protection state could not be confirmed",
    );
    expect(sent).toBe(0);
  });

  it("rechecks automatic protection after the admission storage wait", async () => {
    let reads = 0;
    let sent = 0;
    const root = new SessionProtection({
      async read() {
        return ++reads === 1 ? "clean" : "protected";
      },
      async mark() {},
    });
    const client = new Transport({
      baseUrl: "https://loom.invalid",
      sessionProtection: root,
      logLevel: "off",
      fetch: async () => {
        sent++;
        return json({});
      },
    });
    await expect(client.post("memory.set", {}, { capture: new SessionCapture([root]) })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(sent).toBe(0);
  });

  it("refuses a supplied declaration on stream requests", async () => {
    let sent = 0;
    const client = new Transport({
      baseUrl: "https://loom.invalid",
      sessionProtection: family().root,
      logLevel: "off",
      fetch: async () => {
        sent++;
        return json({});
      },
    });
    await expect(client.stream("/v1/source-events", {}, { headers: { [declaration]: "v1" } })).rejects.toBeInstanceOf(
      SessionProtectionUnavailable,
    );
    expect(sent).toBe(0);
  });

  it("retains independent families across clients and origins", async () => {
    const first = family();
    const second = family();
    const a = new Transport({
      baseUrl: "https://one.invalid",
      sessionProtection: first.root,
      logLevel: "off",
      fetch: async () => json({ mirrored: true }),
    });
    const b = new Transport({
      baseUrl: "https://two.invalid",
      sessionProtection: second.root,
      logLevel: "off",
      fetch: async (_url, init) => {
        expect(new Headers(init.headers).get(declaration)).toBe("v1");
        return json({ ok: true });
      },
    });
    await a.post("knowledge.source.mcp_read", {});
    await expect(b.post("memory.get", {}, { capture: new SessionCapture([second.root]) })).resolves.toEqual({ ok: true });
    await expect(b.post("memory.set", {}, { capture: new SessionCapture([first.root]) })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(second.state()).toBe("clean");
  });

  it.each([200, 403])("withholds malformed private JSON at status %i", async (status) => {
    const source = family();
    const client = new Transport({
      baseUrl: "https://loom.invalid",
      sessionProtection: source.root,
      logLevel: "off",
      fetch: async (_url, init) => {
        expect(new Headers(init.headers).get(declaration)).toBe("v1");
        return new Response(privateValue, { status, headers: { "content-type": "application/json" } });
      },
    });
    await expect(client.post("knowledge.source.mcp_read", {})).rejects.toThrow(
      "Session protection state could not be confirmed",
    );
    expect(source.state()).toBe("protected");
  });

  it("observes stream metadata before output and guards cancellation", async () => {
    const source = family();
    let closed = false;
    const client = new Transport({
      baseUrl: "https://loom.invalid",
      sessionProtection: source.root,
      logLevel: "off",
      fetch: async (_url, init) => {
        expect(new Headers(init.headers).get(declaration)).toBe("v1");
        expect(init.redirect).toBe("error");
        return new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode('data: {"mirrored":true,"value":"private-source-value"}\n\n'));
            },
            cancel() {
              closed = true;
            },
          }),
          { headers: { "content-type": "text/event-stream", "X-Request-Id": "events" } },
        );
      },
    });
    const response = await client.stream("/v1/source-events", {});
    expect(response.headers.get("X-Request-Id")).toBe("events");
    if (!response.body) throw new Error("Expected response body");
    const reader = response.body.getReader();
    const chunk = await reader.read();
    expect(source.state()).toBe("protected");
    expect(new TextDecoder().decode(chunk.value)).toContain(privateValue);
    await reader.cancel();
    reader.releaseLock();
    expect(closed).toBe(true);
    await expect(client.post("memory.set", {}, { capture: new SessionCapture([source.root]) })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
  });
});

describe("browser protection adapter", () => {
  it("preserves HTTP error status, headers and envelope after observation", async () => {
    const source = family();
    const client = protectedBrowserTransport({
      baseUrl: "https://loom.invalid",
      sessionProtection: source.root,
      fetch: async (_url, init) => {
        expect(new Headers(init.headers).get(declaration)).toBe("v1");
        return json({ error: { data: { mirrored: true }, message: "refused" } }, 403);
      },
    });
    const response = await client.fetch("/v1/knowledge.source.mcp_read", {});
    expect(source.state()).toBe("protected");
    expect(response.status).toBe(403);
    expect(response.headers.get("X-Request-Id")).toBe("protected");
    expect(await response.json()).toEqual({ error: { data: { mirrored: true }, message: "refused" } });
    await expect(client.automaticFetch("/v1/memory.set", { method: "POST", body: privateValue })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
  });

  it("never forwards credentials or readiness to another origin", async () => {
    let sent = 0;
    const client = protectedBrowserTransport({
      baseUrl: "https://loom.invalid",
      sessionProtection: family().root,
      fetch: async () => {
        sent++;
        return json({});
      },
    });
    await expect(
      client.fetch("https://upstream.invalid/mcp", { headers: { Authorization: "Bearer secret" } }),
    ).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(sent).toBe(0);
  });

  it("refuses duplicate header tuples and missing restored proof", async () => {
    const source = family();
    let sent = 0;
    const client = protectedBrowserTransport({
      baseUrl: "https://loom.invalid",
      sessionProtection: source.root,
      fetch: async () => {
        sent++;
        return json({});
      },
    });
    await expect(
      client.fetch("/v1/memory.get", {
        headers: [
          [declaration, "v1"],
          [declaration, "v1"],
        ],
      }),
    ).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    source.lose();
    await expect(client.fetch("/v1/memory.get", {})).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(sent).toBe(0);
  });

  it("joins owned cancellation and withholds raw private diagnostics", async () => {
    const source = family();
    const controller = new AbortController();
    let cancelled = false;
    const client = protectedBrowserTransport({
      baseUrl: "https://loom.invalid",
      sessionProtection: source.root,
      fetch: async (_url, init) => {
        expect(new Headers(init.headers).get(declaration)).toBe("v1");
        return new Response(
          new ReadableStream(
            {
              pull() {
                controller.abort();
              },
              cancel() {
                cancelled = true;
              },
            },
            { highWaterMark: 0 },
          ),
          { headers: { "content-type": "application/json" } },
        );
      },
    });
    await expect(
      client.fetch("/v1/knowledge.source.mcp_read", { signal: controller.signal }),
    ).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(source.state()).toBe("protected");
    expect(cancelled).toBe(true);
  });
});
