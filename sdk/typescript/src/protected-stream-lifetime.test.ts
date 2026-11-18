import { describe, expect, it } from "vitest";
import { SessionProtection, SessionProtectionUnavailable } from "./session-protection.js";
import { Transport } from "./transport.js";

const headers = { "content-type": "text/event-stream" };
const encode = (value: string) => new TextEncoder().encode(value);

function controlled() {
  let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>(
    {
      start(value) {
        controller = value;
      },
      cancel() {
        cancelled = true;
      },
    },
    { highWaterMark: 0 },
  );
  return {
    body,
    get controller() {
      if (!controller) throw new Error("missing controller");
      return controller;
    },
    get cancelled() {
      return cancelled;
    },
  };
}

function setup(body: ReadableStream<Uint8Array>, signal?: AbortSignal) {
  const state = { value: "clean" as "clean" | "protected" };
  const root = new SessionProtection({
    async read() {
      return state.value;
    },
    async mark() {
      state.value = "protected";
    },
  });
  const transport = new Transport({
    baseUrl: "http://inert.invalid",
    sessionProtection: root,
    fetch: async () => new Response(body, { headers }),
    logLevel: "off",
  });
  return { root, state, response: transport.stream("/assistant/chat", {}, { signal }) };
}

describe("protected stream lifetime", () => {
  it("delivers an ordinary frame incrementally, then protects a later private frame", async () => {
    const source = controlled();
    const { state, response } = setup(source.body);
    const reader = (await response).body?.getReader();
    if (!reader) throw new Error("missing body");
    const ordinary = 'data: {"text":"ordinary"}\n\n';
    const first = reader.read();
    source.controller.enqueue(encode(ordinary));
    expect((await first).value).toEqual(encode(ordinary));
    expect(state.value).toBe("clean");
    const second = reader.read();
    const privateFrame = 'data: {"mirrored":true,"text":"private"}\n\n';
    source.controller.enqueue(encode(privateFrame));
    expect((await second).value).toEqual(encode(privateFrame));
    expect(state.value).toBe("protected");
    source.controller.close();
    expect((await reader.read()).done).toBe(true);
  });

  it("protects and closes on cancellation before the first pull", async () => {
    const source = controlled();
    const { state, response } = setup(source.body);
    await (await response).body?.cancel();
    expect(state.value).toBe("protected");
    expect(source.cancelled).toBe(true);
    expect(source.body.locked).toBe(false);
  });

  it.each([false, true])("handles caller abort with pendingRead=%s", async (pendingRead) => {
    const source = controlled();
    const abort = new AbortController();
    const { state, response } = setup(source.body, abort.signal);
    const reader = (await response).body?.getReader();
    if (!reader) throw new Error("missing body");
    const output = pendingRead
      ? reader.read().then(
          () => undefined,
          (error: unknown) => error,
        )
      : undefined;
    abort.abort();
    const error = output ? await output : await reader.read().catch((error: unknown) => error);
    expect(error).toBeInstanceOf(DOMException);
    expect((error as DOMException).name).toBe("AbortError");
    expect(state.value).toBe("protected");
    expect(source.cancelled).toBe(true);
    await reader.cancel().catch(() => undefined);
  });

  it.each([new Error("inert connection failure"), new TypeError("inert connection failure")])(
    "retains the original network failure and protects before returning it (%#)",
    async (failure) => {
      const source = controlled();
      const { state, response } = setup(source.body);
      const output = (await response).text().then(
        () => undefined,
        (error: unknown) => error,
      );
      source.controller.error(failure);
      expect(await output).toBe(failure);
      expect(state.value).toBe("protected");
    },
  );

  it("withholds output when a durable store does not acknowledge before the deadline", async () => {
    let acknowledge: (() => void) | undefined;
    let state: "clean" | "protected" = "clean";
    const root = new SessionProtection({
      async read() {
        return state;
      },
      async mark() {
        await new Promise<void>((resolve) => {
          acknowledge = resolve;
        });
        state = "protected";
      },
    });
    const source = controlled();
    const transport = new Transport({
      baseUrl: "http://inert.invalid",
      timeoutMs: 25,
      sessionProtection: root,
      fetch: async () => new Response(source.body, { headers }),
      logLevel: "off",
    });
    const response = await transport.stream("/assistant/chat", {});
    const output = response.text().then(
      () => undefined,
      (error: unknown) => error,
    );
    source.controller.enqueue(encode('data: {"mirrored":true,"text":"private"}\n\n'));
    try {
      expect(await output).toBeInstanceOf(SessionProtectionUnavailable);
      expect(root.locallyProtected).toBe(true);
      expect(source.cancelled).toBe(true);
    } finally {
      acknowledge?.();
    }
  });
  it.each(["mark", "cancel"])("joins pending %s before returning a malformed-frame failure", async (pending) => {
    let entered: (() => void) | undefined;
    let release: (() => void) | undefined;
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const held = () =>
      new Promise<void>((resolve) => {
        release = resolve;
        entered?.();
      });
    let state: "clean" | "protected" = "clean";
    const root = new SessionProtection({
      async read() {
        return state;
      },
      async mark() {
        if (pending === "mark" && state === "clean") await held();
        state = "protected";
      },
    });
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encode("data: []\n\n"));
      },
      async cancel() {
        if (pending === "cancel") await held();
      },
    });
    const transport = new Transport({
      baseUrl: "http://inert.invalid",
      sessionProtection: root,
      fetch: async () => new Response(body, { headers }),
      logLevel: "off",
    });
    let returned = false;
    const output = transport
      .stream("/assistant/chat", {})
      .then((value) => value.text())
      .then(
        () => {
          returned = true;
          return undefined;
        },
        (error: unknown) => {
          returned = true;
          return error;
        },
      );
    try {
      await started;
      // Drain reactions through an event-loop turn while the cleanup remains held.
      await new Promise<void>((resolve) => setImmediate(resolve));
      expect(root.locallyProtected).toBe(true);
      expect(returned).toBe(false);
    } finally {
      release?.();
    }
    expect(await output).toBeInstanceOf(SessionProtectionUnavailable);
  });
});
