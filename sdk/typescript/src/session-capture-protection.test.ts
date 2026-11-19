import { describe, expect, it } from "vitest";
import { Agent } from "./agent.js";
import { BackendUnavailable } from "./errors.js";
import {
  AutomaticCaptureSuppressed,
  SessionCapture,
  SessionProtection,
  SessionProtectionUnavailable,
} from "./session-protection.js";

function family() {
  const state = { value: "clean" as "clean" | "protected" };
  const root = new SessionProtection({
    async read() {
      return state.value;
    },
    async mark() {
      state.value = "protected";
    },
  });
  return { root, state };
}

function client(root: SessionProtection, reply: (op: string) => unknown = () => ({})) {
  const ops: string[] = [];
  const bot = new Agent("o", "n", "a", {
    baseUrl: "http://inert.invalid",
    sessionProtection: root,
    logLevel: "off",
    fetch: async (url) => {
      const op = url.split("/v1/")[1];
      ops.push(op);
      return new Response(JSON.stringify(reply(op)), { headers: { "content-type": "application/json" } });
    },
  });
  return { bot, ops };
}

describe("Session retained protection", () => {
  it("suppresses automatic transcript and extraction before dispatch", async () => {
    const source = family();
    await source.root.mark();
    const { bot, ops } = client(source.root);
    const session = bot.session("s", { flushEvery: 1 });
    await expect(session.append({ role: "user", content: "private" })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(ops).toEqual([]);
    expect(session.buffered).toEqual([]);
  });

  it("discards buffered text as soon as its retained family is protected", async () => {
    const source = family();
    const { bot, ops } = client(source.root);
    const session = bot.session("s", { transcript: false, flushEvery: 0 });
    await session.append({ role: "user", content: "private" });
    expect(session.buffered).toHaveLength(1);
    await source.root.mark();
    expect(session.buffered).toEqual([]);
    await session.close();
    expect(ops).toEqual([]);
  });

  it("does not extract buffered turns after a different client protects their family", async () => {
    const source = family();
    const { bot, ops } = client(source.root);
    const session = bot.session("s", { transcript: false, flushEvery: 0 });
    await session.append({ role: "user", content: "private" });
    await source.root.mark();
    await expect(session.flush()).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(session.buffered).toEqual([]);
    expect(ops).toEqual([]);
  });

  it("returns a marked recall without issuing optional transcript history", async () => {
    const source = family();
    const { bot, ops } = client(source.root, (op) =>
      op === "context.pack" ? { rendered: "private", mirrored: true } : { found: true, messages: [] },
    );
    const out = await bot.session("s").resume();
    expect(out.system).toBe("private");
    expect(out.messages).toEqual([]);
    expect(source.state.value).toBe("protected");
    expect(ops).toEqual(["context.pack"]);
  });

  it("keeps explicit offload, fetch and note available after protection", async () => {
    const source = family();
    await source.root.mark();
    const { bot, ops } = client(source.root);
    const session = bot.session("s");
    await session.offload("chosen");
    await session.fetch("blob/key");
    await session.note("chosen");
    expect(ops).toEqual(["context.offload", "context.fetch", "working_memory.append"]);
  });

  it("discards extraction retry and carry-over text when the response protects the family", async () => {
    const source = family();
    const { bot } = client(source.root, () => ({ mirrored: true, written: [] }));
    const session = bot.session("s", { transcript: false, flushEvery: 1 });
    await expect(session.append({ role: "user", content: "private" })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(session.buffered).toEqual([]);
    expect(await session.close()).toBeUndefined();
  });

  it("retains the creating family when the Agent client is rebound", async () => {
    const first = family();
    const second = family();
    const original = client(first.root);
    const replacement = client(second.root);
    const session = original.bot.session("s", { transcript: false, flushEvery: 0 });
    Object.defineProperty(original.bot, "client", { value: replacement.bot.client });
    await first.root.mark();
    await expect(session.append({ role: "user", content: "private" })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(replacement.ops).toEqual([]);
    expect(session.buffered).toEqual([]);
  });

  it("observes explicit responses for both retained and currently bound families", async () => {
    const first = family();
    const second = family();
    const original = client(first.root);
    const replacement = client(second.root, () => ({ mirrored: true }));
    const session = original.bot.session("s");
    Object.defineProperty(original.bot, "client", { value: replacement.bot.client });
    await session.fetch("blob/key");
    expect(first.state.value).toBe("protected");
    expect(second.state.value).toBe("protected");
    expect(replacement.ops).toEqual(["context.fetch"]);
  });
  it("sends no prior batch when carryOver is zero", async () => {
    const source = family();
    const requests: Record<string, unknown>[] = [];
    const bot = new Agent("o", "n", "a", {
      baseUrl: "http://inert.invalid",
      sessionProtection: source.root,
      logLevel: "off",
      fetch: async (_url, init) => {
        requests.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return new Response("{}", { headers: { "content-type": "application/json" } });
      },
    });
    const session = bot.session("s", { transcript: false, flushEvery: 1, carryOver: 0 });
    await session.append({ role: "user", content: "first" });
    await session.append({ role: "user", content: "second" });
    expect(requests).toHaveLength(2);
    expect(requests[1].messages).toEqual([{ role: "user", content: "second" }]);
    expect(requests[1]).not.toHaveProperty("last_messages");
  });
  it("freezes constructor contributors when the caller replaces its options", async () => {
    const first = family();
    const second = family();
    const replacement = family();
    const { bot, ops } = client(second.root);
    const options = { capture: new SessionCapture([first.root]), transcript: false, flushEvery: 0 };
    const session = bot.session("s", options);
    options.capture = new SessionCapture([replacement.root]);
    await first.root.mark();
    await expect(session.append({ role: "user", content: "private" })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(ops).toEqual([]);
  });

  it("bounds an unavailable store before buffering or dispatching a turn", async () => {
    let release: (() => void) | undefined;
    const waiting = new Promise<void>((resolve) => {
      release = resolve;
    });
    const root = new SessionProtection({
      async read() {
        await waiting;
        return "protected";
      },
      async mark() {},
    });
    let sent = 0;
    const bot = new Agent("o", "n", "a", {
      baseUrl: "http://inert.invalid",
      timeoutMs: 20,
      sessionProtection: root,
      logLevel: "off",
      fetch: async () => {
        sent++;
        return new Response("{}");
      },
    });
    const session = bot.session("s", { transcript: false, flushEvery: 0 });
    try {
      await expect(session.append({ role: "user", content: "private" })).rejects.toBeInstanceOf(
        SessionProtectionUnavailable,
      );
      expect(session.buffered).toEqual([]);
      expect(sent).toBe(0);
    } finally {
      release?.();
    }
  });

  it("keeps a backend failure primary when a fresh durable check disallows retrying its batch", async () => {
    const source = family();
    const bot = new Agent("o", "n", "a", {
      baseUrl: "http://inert.invalid",
      retry: { maxAttempts: 1 },
      sessionProtection: source.root,
      logLevel: "off",
      fetch: async () => {
        source.state.value = "protected";
        return new Response(JSON.stringify({ detail: { code: "INERT", message: "unavailable" } }), { status: 503 });
      },
    });
    const session = bot.session("s", { transcript: false, flushEvery: 1 });
    await expect(session.append({ role: "user", content: "private" })).rejects.toBeInstanceOf(BackendUnavailable);
    expect(session.buffered).toEqual([]);
    expect(await session.close()).toBeUndefined();
  });
  it("retains a completed pack when optional history is suppressed during authentication", async () => {
    const source = family();
    let tokens = 0;
    const ops: string[] = [];
    const bot = new Agent("o", "n", "a", {
      baseUrl: "http://inert.invalid",
      sessionProtection: source.root,
      logLevel: "off",
      tokenProvider: {
        async token() {
          if (++tokens === 2) await source.root.mark();
          return "inert";
        },
      },
      fetch: async (url) => {
        ops.push(url.split("/v1/")[1]);
        return new Response(JSON.stringify({ rendered: "completed pack" }));
      },
    });
    const out = await bot.session("s").resume();
    expect(out.system).toBe("completed pack");
    expect(out.messages).toEqual([]);
    expect(ops).toEqual(["context.pack"]);
    expect(tokens).toBe(2);
  });

  it("returns the first marked history and suppresses the next automatic read", async () => {
    const source = family();
    const messages = [{ role: "user", content: "private" }];
    const { bot, ops } = client(source.root, () => ({ mirrored: true, found: true, messages }));
    const session = bot.session("s");
    expect(await session.history()).toEqual(messages);
    await expect(session.history()).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(ops).toEqual(["conversation.get"]);
  });

  it("requeues a clean failed batch before turns appended during that request", async () => {
    const source = family();
    let entered: (() => void) | undefined;
    let release: (() => void) | undefined;
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const requests: Record<string, unknown>[] = [];
    const bot = new Agent("o", "n", "a", {
      baseUrl: "http://inert.invalid",
      sessionProtection: source.root,
      logLevel: "off",
      retry: { maxAttempts: 1 },
      fetch: async (_url, init) => {
        requests.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        if (requests.length === 1) {
          entered?.();
          await held;
          return new Response("{}", { status: 503 });
        }
        return new Response("{}");
      },
    });
    const session = bot.session("s", { transcript: false, flushEvery: 0 });
    await session.append({ role: "user", content: "old" });
    const output = session.flush().then(
      () => undefined,
      (error: unknown) => error,
    );
    try {
      await started;
      await session.append({ role: "user", content: "new" });
    } finally {
      release?.();
    }
    expect(await output).toBeInstanceOf(BackendUnavailable);
    expect(session.buffered.map((turn) => turn.content)).toEqual(["old", "new"]);
    await session.flush();
    expect(requests[1].messages).toEqual([
      { role: "user", content: "old" },
      { role: "user", content: "new" },
    ]);
  });

  it("protects a retained producer on malformed explicit output after unbinding", async () => {
    const source = family();
    const { bot } = client(source.root);
    const session = bot.session("s", { transcript: false, flushEvery: 0 });
    await session.append({ role: "user", content: "private" });
    const unbound = new Agent("o", "n", "a", {
      baseUrl: "http://inert.invalid",
      logLevel: "off",
      fetch: async () => new Response('{"mirrored":true'),
    });
    Object.defineProperty(bot, "client", { value: unbound.client });
    await expect(session.fetch("blob/key")).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(source.state.value).toBe("protected");
    expect(session.buffered).toEqual([]);
  });
});
