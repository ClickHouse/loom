import { describe, expect, it, vi } from "vitest";
import { LoomClient } from "./client.js";
import {
  AutomaticCaptureSuppressed,
  SessionCapture,
  SessionProtection,
  type SessionProtectionStore,
  SessionProtectionUnavailable,
} from "./session-protection.js";
import { Transport } from "./transport.js";

function family() {
  const state = { value: "clean" as "clean" | "protected", marks: 0 };
  const store: SessionProtectionStore = {
    async read() {
      return state.value;
    },
    async mark() {
      state.marks++;
      state.value = "protected";
    },
  };
  const root = new SessionProtection(store);
  return { state, root };
}

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });

it.each(["json", "stream"])("rechecks an earlier family after a sibling read was pending for %s", async (route) => {
  const first = family();
  let entered: (() => void) | undefined;
  let release: (() => void) | undefined;
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const second = new SessionProtection({
    async read() {
      entered?.();
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return "clean";
    },
    async mark() {},
  });
  let sends = 0;
  const options = {
    baseUrl: "http://inert.invalid",
    sessionProtection: first.root,
    fetch: async () => {
      sends++;
      return json({ found: false });
    },
    logLevel: "off" as const,
  };
  const capture = new SessionCapture([first.root, second]);
  const request =
    route === "stream"
      ? new Transport(options).stream("/assistant/chat", {}, { capture })
      : new LoomClient(options).call("knowledge.get", { org: "o", namespace: "n", source_uri: "doc" }, { capture });
  const call = request.then(
    (value) => value,
    (error: unknown) => error,
  );
  try {
    await started;
    await first.root.mark();
    release?.();
    expect(await call).toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(sends).toBe(0);
  } finally {
    release?.();
    await call;
  }
});

it("cancels a bound raw stream before refusing after caller abort", async () => {
  let entered: (() => void) | undefined;
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  let release: (() => void) | undefined;
  const root = new SessionProtection({
    async read() {
      return "protected";
    },
    async mark() {
      entered?.();
      await new Promise<void>((resolve) => {
        release = resolve;
      });
    },
  });
  let cancelled = false;
  const controller = new AbortController();
  const stream = new ReadableStream({
    cancel() {
      cancelled = true;
    },
  });
  const transport = new Transport({
    baseUrl: "http://inert.invalid",
    sessionProtection: root,
    fetch: async () => new Response(stream),
    logLevel: "off",
  });
  const call = transport.stream("/assistant/chat", {}, { signal: controller.signal }).catch((error: unknown) => error);
  try {
    await started;
    controller.abort();
    expect(await call).toBeInstanceOf(SessionProtectionUnavailable);
    expect(cancelled).toBe(true);
  } finally {
    release?.();
    await call;
  }
});

it("preserves an unbound raw response for its caller", async () => {
  const response = new Response("data: ordinary\n\n");
  const transport = new Transport({ baseUrl: "http://inert.invalid", fetch: async () => response, logLevel: "off" });
  expect(await transport.stream("/assistant/chat", {})).toBe(response);
  expect(await response.text()).toBe("data: ordinary\n\n");
});

describe("bound transport observation", () => {
  it("returns a marked result only after its durable mark is acknowledged", async () => {
    let marked = false;
    let acknowledge: (() => void) | undefined;
    let enteredMark: (() => void) | undefined;
    const markStarted = new Promise<void>((resolve) => {
      enteredMark = resolve;
    });
    const root = new SessionProtection({
      async read() {
        return marked ? "protected" : "clean";
      },
      async mark() {
        enteredMark?.();
        await new Promise<void>((resolve) => {
          acknowledge = resolve;
        });
        marked = true;
      },
    });
    const client = new LoomClient({
      baseUrl: "http://inert.invalid",
      sessionProtection: root,
      fetch: async () => json({ mirrored: true, found: true, content: "private" }),
      logLevel: "off",
    });
    let returned = false;
    let fetches = 0;
    const automatic = new LoomClient({
      baseUrl: "http://inert.invalid",
      sessionProtection: root,
      fetch: async () => {
        fetches++;
        return json({ found: false });
      },
      logLevel: "off",
    });
    const call = client.call("knowledge.get", { org: "o", namespace: "n", source_uri: "doc" }).then((value) => {
      returned = true;
      return value;
    });
    const first = await Promise.race([markStarted.then(() => "mark"), call.then(() => "return")]);
    expect(first).toBe("mark");
    expect(returned).toBe(false);
    await expect(
      automatic.call(
        "knowledge.get",
        { org: "o", namespace: "n", source_uri: "doc" },
        {
          capture: new SessionCapture([root]),
        },
      ),
    ).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(fetches).toBe(0);
    expect(acknowledge).toBeTypeOf("function");
    acknowledge?.();
    expect((await call).content).toBe("private");
    expect(marked).toBe(true);
  });

  it("protects an automatic producer before considering a marked error retry", async () => {
    const { state, root } = family();
    let calls = 0;
    const client = new LoomClient({
      baseUrl: "http://inert.invalid",
      sessionProtection: root,
      retry: { maxAttempts: 2, backoffMs: 0, backoffJitter: 0 },
      logLevel: "off",
      fetch: async () => {
        calls++;
        return json({ mirrored: true, detail: "private refusal" }, 503);
      },
    });
    await expect(
      client.call(
        "knowledge.get",
        { org: "o", namespace: "n", source_uri: "doc" },
        {
          capture: new SessionCapture([root]),
        },
      ),
    ).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(state.value).toBe("protected");
    expect(calls).toBe(1);
  });

  it("rechecks the captured producer after awaiting credentials", async () => {
    const { state, root } = family();
    let calls = 0;
    const client = new LoomClient({
      baseUrl: "http://inert.invalid",
      sessionProtection: root,
      tokenProvider: {
        origin: "http://inert.invalid",
        async token() {
          state.value = "protected";
          return "token";
        },
      },
      fetch: async () => {
        calls++;
        return json({ found: false });
      },
      logLevel: "off",
    });
    await expect(
      client.call(
        "knowledge.get",
        { org: "o", namespace: "n", source_uri: "doc" },
        {
          capture: new SessionCapture([root]),
        },
      ),
    ).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(calls).toBe(0);
  });

  it("attempts every known durable mark even when an earlier family fails", async () => {
    const bad = new SessionProtection({
      async read() {
        return "clean";
      },
      async mark() {
        throw new Error("unavailable");
      },
    });
    const { state, root } = family();
    const client = new LoomClient({
      baseUrl: "http://inert.invalid",
      sessionProtection: bad,
      fetch: async () => json({ mirrored: true, content: "private" }),
      logLevel: "off",
    });
    await expect(
      client.call(
        "knowledge.get",
        { org: "o", namespace: "n", source_uri: "doc" },
        {
          capture: new SessionCapture([bad, root]),
        },
      ),
    ).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(state.value).toBe("protected");
  });

  it("withholds malformed bound bodies with a constant error and protects", async () => {
    const { state, root } = family();
    const client = new LoomClient({
      baseUrl: "http://inert.invalid",
      sessionProtection: root,
      fetch: async () => new Response('{"mirrored":true,"content":"private', { status: 200 }),
      logLevel: "off",
    });
    await expect(client.call("knowledge.get", { org: "o", namespace: "n", source_uri: "doc" })).rejects.toThrow(
      "Session protection state could not be confirmed",
    );
    expect(state.value).toBe("protected");
  });

  it("keeps explicit calls available after protection and leaves ordinary shapes intact", async () => {
    const { state, root } = family();
    state.value = "protected";
    const client = new LoomClient({
      baseUrl: "http://inert.invalid",
      sessionProtection: root,
      fetch: async () => json({ found: false }),
      logLevel: "off",
    });
    expect(await client.call("knowledge.get", { org: "o", namespace: "n", source_uri: "doc" })).toEqual({
      found: false,
    });
    expect(state.marks).toBe(0);
  });
});

it("a pending first mark cannot starve another family or ignore caller abort", async () => {
  let state: "clean" | "protected" = "clean";
  let entered: (() => void) | undefined;
  let release: (() => void) | undefined;
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const first = new SessionProtection({
    async read() {
      return state;
    },
    async mark() {
      entered?.();
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      state = "protected";
    },
  });
  const second = family();
  const controller = new AbortController();
  const client = new LoomClient({
    baseUrl: "http://inert.invalid",
    sessionProtection: first,
    fetch: async () => json({ mirrored: true, content: "private" }),
    logLevel: "off",
  });
  let settled = false;
  const call = client
    .call(
      "knowledge.get",
      { org: "o", namespace: "n", source_uri: "doc" },
      {
        capture: new SessionCapture([first, second.root]),
        signal: controller.signal,
      },
    )
    .then(
      (value) => {
        settled = true;
        return value;
      },
      (error: unknown) => {
        settled = true;
        return error;
      },
    );
  try {
    await started;
    controller.abort();
    await new Promise<void>((resolve) => setImmediate(resolve));
    expect({ sibling: second.state.value, settled }).toEqual({ sibling: "protected", settled: true });
    expect(await call).toBeInstanceOf(SessionProtectionUnavailable);
    expect(await first.protected()).toBe(true);
  } finally {
    release?.();
    await call;
  }
});

it("a pending automatic state read honors the call timeout before fetch", async () => {
  vi.useFakeTimers();
  let entered: (() => void) | undefined;
  let release: (() => void) | undefined;
  let released = false;
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const root = new SessionProtection({
    async read() {
      if (!released) {
        entered?.();
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      }
      return "clean";
    },
    async mark() {},
  });
  let sends = 0;
  const client = new LoomClient({
    baseUrl: "http://inert.invalid",
    sessionProtection: root,
    fetch: async () => {
      sends++;
      return json({ found: false });
    },
    logLevel: "off",
  });
  let settled = false;
  const call = client
    .call(
      "knowledge.get",
      { org: "o", namespace: "n", source_uri: "doc" },
      {
        capture: new SessionCapture([root]),
        timeoutMs: 20,
      },
    )
    .then(
      (value) => {
        settled = true;
        return value;
      },
      (error: unknown) => {
        settled = true;
        return error;
      },
    );
  try {
    await started;
    await vi.advanceTimersByTimeAsync(20);
    expect(settled).toBe(true);
    expect(await call).toBeInstanceOf(SessionProtectionUnavailable);
    expect(await root.protected()).toBe(true);
    expect(sends).toBe(0);
  } finally {
    released = true;
    release?.();
    await call;
    vi.useRealTimers();
  }
});

it.each([200, 204])("empty bound HTTP %s preserves only a legitimate bodyless status", async (status) => {
  const { state, root } = family();
  const client = new LoomClient({
    baseUrl: "http://inert.invalid",
    sessionProtection: root,
    fetch: async () => new Response(null, { status }),
    logLevel: "off",
  });
  const call = client.call("knowledge.get", { org: "o", namespace: "n", source_uri: "doc" });
  if (status === 204) {
    expect(await call).toEqual({});
    expect(state.value).toBe("clean");
  } else {
    await expect(call).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(state.value).toBe("protected");
  }
});
