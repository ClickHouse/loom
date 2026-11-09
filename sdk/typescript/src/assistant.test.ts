import { describe, expect, expectTypeOf, it } from "vitest";
import {
  ASSISTANT_ROUTE,
  type AskInput,
  type AssistantChat,
  type AssistantCitation,
  type AssistantEvent,
  type AssistantTurn,
} from "./assistant.js";
import { LoomClient } from "./client.js";
import { AuthError, BackendUnavailable, LoomError, RateLimited, TimeoutError } from "./errors.js";
import type { TokenProvider } from "./machine.js";
import { Transport } from "./transport.js";

const CITED = [{ kind: "memory", text: "[memory:a/b/c@v3]", verified: true, reason: "", ref: "a/b/c" }];

function sse(...events: unknown[]): string {
  return `${events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("")}data: [DONE]\n\n`;
}

const chunk = (text: string) => ({ id: "chatcmpl-1", model: "m", choices: [{ index: 0, delta: { content: text } }] });

const ANSWER = sse(
  chunk("Deploys freeze "),
  chunk("on Fridays."),
  { id: "chatcmpl-1", choices: [], usage: { prompt_tokens: 9, completion_tokens: 4 } },
  { object: "loom.citations", id: "chatcmpl-1", citations: CITED },
);

/** A body delivered in pieces cut at `cuts`, so an event can span reads. */
function body(text: string, cuts: number[] = [], fail?: Error): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(text);
  const bounds = [0, ...cuts, bytes.length];
  let i = 0;
  return new ReadableStream({
    pull(ctrl) {
      if (i < bounds.length - 1) {
        ctrl.enqueue(bytes.slice(bounds[i], bounds[i + 1]));
        i += 1;
      } else if (fail) ctrl.error(fail);
      else ctrl.close();
    },
  });
}

function client(reply: () => Response) {
  const sent: Array<{ url: string; init: RequestInit }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    sent.push({ url, init });
    return reply();
  };
  const transport = new Transport({ baseUrl: "http://loom.test", apiKey: "k", fetch, logLevel: "off" });
  return { loom: new LoomClient(transport), sent };
}

async function collect(
  loom: LoomClient,
  input: AskInput = { namespace: "handbook", question: "When are deploys frozen?" },
) {
  const out: AssistantEvent[] = [];
  for await (const e of loom.assistant.stream(input)) out.push(e);
  return out;
}

describe("loom.assistant.stream", () => {
  it("yields the text as it arrives, then the answer with its checked citations", async () => {
    const { loom, sent } = client(() => new Response(body(ANSWER, [7, 40, 41, 200])));
    const history = [
      { role: "user" as const, content: "hi" },
      { role: "assistant" as const, content: "Hello." },
    ];
    const out: AssistantEvent[] = [];
    for await (const e of loom.assistant.stream({ namespace: "handbook", question: "q", history })) out.push(e);
    expect(out.slice(0, 2)).toEqual([
      { type: "text", text: "Deploys freeze " },
      { type: "text", text: "on Fridays." },
    ]);
    expect(out[2]).toEqual({
      type: "answer",
      answer: {
        text: "Deploys freeze on Fridays.",
        runId: "chatcmpl-1",
        model: "m",
        citations: CITED,
        promptTokens: 9,
        completionTokens: 4,
      },
    });
    expect(out).toHaveLength(3);
    const [{ url, init }] = sent;
    expect(url).toBe(`http://loom.test${ASSISTANT_ROUTE}`);
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Loom-Namespace"]).toBe("handbook");
    expect(headers.Authorization).toBe("Bearer k");
    expect(JSON.parse(String(init.body))).toEqual({
      stream: true,
      messages: [...history, { role: "user", content: "q" }],
    });
  });

  it("throws a refusal's typed error and never resends it", async () => {
    const detail = { code: "RATE_LIMITED", message: "the assistant's share is spent" };
    const { loom, sent } = client(
      () => new Response(JSON.stringify({ detail }), { status: 429, headers: { "Retry-After": "60" } }),
    );
    await expect(collect(loom)).rejects.toBeInstanceOf(RateLimited);
    expect(sent).toHaveLength(1);
  });

  it("throws a 403 as an AuthError", async () => {
    const detail = { code: "AUTH_INSUFFICIENT", message: "no viewer" };
    const { loom } = client(() => new Response(JSON.stringify({ detail }), { status: 403 }));
    await expect(collect(loom)).rejects.toBeInstanceOf(AuthError);
  });

  it("throws an error event after the text as its typed error", async () => {
    const error = { code: "ASSISTANT_UPSTREAM_FAILED", message: "LibreChat failed", status: 503 };
    const { loom } = client(() => new Response(body(sse(chunk("Deploys "), { error }))));
    const seen: AssistantEvent[] = [];
    const run = async () => {
      for await (const e of loom.assistant.stream({ question: "q" })) seen.push(e);
    };
    const err = await run().then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(BackendUnavailable);
    expect((err as LoomError).code).toBe("ASSISTANT_UPSTREAM_FAILED");
    expect(seen).toEqual([{ type: "text", text: "Deploys " }]);
  });

  it("throws an error event with no readable status, as a build without it sends, as a 503", async () => {
    for (const status of [undefined, "busy", 0]) {
      const error = { code: "ASSISTANT_UPSTREAM_FAILED", message: "LibreChat failed", status };
      const { loom } = client(() => new Response(body(sse(chunk("Deploys "), { error }))));
      const err = await collect(loom).then(
        () => undefined,
        (e: unknown) => e,
      );
      expect(err).toBeInstanceOf(BackendUnavailable);
      expect((err as BackendUnavailable).status).toBe(503);
    }
  });

  it("sends a 401 once more with the provider's fresh token, and no more", async () => {
    const asked: Array<string | undefined> = [];
    const tokens = ["old", "new"];
    const provider: TokenProvider = {
      token: (refused) => {
        asked.push(refused);
        return tokens.shift() ?? "new";
      },
    };
    const sent: string[] = [];
    const fetch = async (_url: string, init: RequestInit) => {
      const auth = (init.headers as Record<string, string>).Authorization;
      sent.push(auth);
      if (auth === "Bearer old")
        return new Response(JSON.stringify({ detail: { code: "AUTH_INVALID" } }), { status: 401 });
      return new Response(body(ANSWER));
    };
    const loom = new LoomClient(
      new Transport({ baseUrl: "http://loom.test", tokenProvider: provider, fetch, logLevel: "off" }),
    );
    expect((await collect(loom)).at(-1)?.type).toBe("answer");
    expect(sent).toEqual(["Bearer old", "Bearer new"]);
    expect(asked).toEqual([undefined, "old"]);
  });

  it("throws a second 401, after the fresh token, without a third send", async () => {
    let sends = 0;
    const fetch = async () => {
      sends += 1;
      return new Response(JSON.stringify({ detail: { code: "AUTH_INVALID" } }), { status: 401 });
    };
    const tokens = ["old", "new"];
    const provider: TokenProvider = { token: () => tokens.shift() ?? "newer" };
    const transport = new Transport({ baseUrl: "http://loom.test", tokenProvider: provider, fetch, logLevel: "off" });
    await expect(collect(new LoomClient(transport))).rejects.toBeInstanceOf(AuthError);
    expect(sends).toBe(2);
  });

  it("skips a chunk that is not JSON, and reads usage that is not a number as 0", async () => {
    const text = `data: not json\n\n${sse(chunk("Deploys "), { id: "chatcmpl-1", choices: [], usage: { prompt_tokens: "many" } })}`;
    const { loom } = client(() => new Response(body(text)));
    const last = (await collect(loom)).at(-1);
    expect(last?.type === "answer" && [last.answer.text, last.answer.promptTokens]).toEqual(["Deploys ", 0]);
  });

  it("does not count the caller's own time on an event against readTimeoutMs", async () => {
    // As fetch does: once the request is aborted, the next read of its body fails.
    const pieces = ANSWER.split(/(?<=\n\n)/).map((e) => new TextEncoder().encode(e));
    const fetch = async (_url: string, init: RequestInit) =>
      new Response(
        new ReadableStream<Uint8Array>({
          pull(c) {
            if (init.signal?.aborted) c.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
            else {
              const next = pieces.shift();
              if (next) c.enqueue(next);
              else c.close();
            }
          },
        }),
      );
    const loom = new LoomClient(new Transport({ baseUrl: "http://loom.test", apiKey: "k", fetch, logLevel: "off" }));
    const seen: string[] = [];
    // A bound wide enough for a loaded runner, the caller several times slower than it.
    for await (const e of loom.assistant.stream({ question: "q", readTimeoutMs: 200 })) {
      seen.push(e.type);
      await new Promise((r) => setTimeout(r, 500));
    }
    expect(seen.at(-1)).toBe("answer");
  });

  it("throws a 401 when the provider has no fresh token", async () => {
    let sends = 0;
    const fetch = async () => {
      sends += 1;
      return new Response(JSON.stringify({ detail: { code: "AUTH_INVALID" } }), { status: 401 });
    };
    const provider: TokenProvider = { token: () => "same" };
    const loom = new LoomClient(
      new Transport({ baseUrl: "http://loom.test", tokenProvider: provider, fetch, logLevel: "off" }),
    );
    await expect(collect(loom)).rejects.toBeInstanceOf(AuthError);
    expect(sends).toBe(1);
  });

  it("refuses to send a provider's token to another origin", async () => {
    let sends = 0;
    const fetch = async () => {
      sends += 1;
      return new Response(body(ANSWER));
    };
    const provider: TokenProvider = { token: () => "t", origin: "http://other.test" };
    const loom = new LoomClient(
      new Transport({ baseUrl: "http://loom.test", tokenProvider: provider, fetch, logLevel: "off" }),
    );
    await expect(collect(loom)).rejects.toBeInstanceOf(AuthError);
    expect(sends).toBe(0);
  });

  it("throws a TimeoutError when no data arrives within readTimeoutMs", async () => {
    // As fetch does: an abort errors the body being read.
    const fetch = async (_url: string, init: RequestInit) => {
      let sentFirst = false;
      const stream = new ReadableStream<Uint8Array>({
        pull(c) {
          if (!sentFirst) {
            sentFirst = true;
            c.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(chunk("Deploys "))}\n\n`));
            return;
          }
          return new Promise<void>((resolve) =>
            init.signal?.addEventListener("abort", () => {
              c.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
              resolve();
            }),
          );
        },
      });
      return new Response(stream);
    };
    const loom = new LoomClient(new Transport({ baseUrl: "http://loom.test", apiKey: "k", fetch, logLevel: "off" }));
    const err = await collect(loom, { question: "q", readTimeoutMs: 50 }).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(TimeoutError);
  });

  it("throws a TimeoutError when the token provider does not answer within readTimeoutMs", async () => {
    const provider: TokenProvider = { token: () => new Promise<string>(() => {}) };
    const fetch = async () => new Response(body(ANSWER));
    const loom = new LoomClient(
      new Transport({ baseUrl: "http://loom.test", tokenProvider: provider, fetch, logLevel: "off" }),
    );
    await expect(collect(loom, { question: "q", readTimeoutMs: 50 })).rejects.toBeInstanceOf(TimeoutError);
  });

  it("throws when the stream ends before the answer began", async () => {
    const { loom } = client(() => new Response("data: [DONE]\n\n"));
    await expect(collect(loom)).rejects.toThrow(/before the answer began/);
  });

  it("throws a connection cut mid-stream as NETWORK", async () => {
    const { loom } = client(() => new Response(body(sse(chunk("Deploys ")), [5], new Error("reset"))));
    const err = await collect(loom).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(BackendUnavailable);
    expect((err as LoomError).code).toBe("NETWORK");
  });

  it("throws the caller's abort mid-stream as an abort, not a cut connection", async () => {
    const ctrl = new AbortController();
    const reason = Object.assign(new Error("aborted"), { name: "AbortError" });
    let pulls = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(c) {
        pulls += 1;
        if (pulls === 1) c.enqueue(new TextEncoder().encode(sse(chunk("Deploys ")).split("data: [DONE]")[0]));
        else {
          ctrl.abort();
          c.error(reason);
        }
      },
    });
    const { loom } = client(() => new Response(stream));
    const err = await collect(loom, { namespace: "n", question: "q", signal: ctrl.signal }).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(LoomError);
    expect(err).not.toBeInstanceOf(BackendUnavailable);
    expect((err as LoomError).message).toMatch(/aborted by caller/);
  });

  it("keeps the generated op beside the stream", () => {
    const { loom } = client(() => new Response("{}"));
    expect(typeof loom.assistant.ask).toBe("function");
    expect(typeof loom.assistant.stream).toBe("function");
  });

  it("types the stream's input and events", () => {
    const { loom } = client(() => new Response("{}"));
    expectTypeOf(loom.assistant.stream).toEqualTypeOf<AssistantChat["stream"]>();
    expectTypeOf<AskInput["history"]>().toEqualTypeOf<AssistantTurn[] | undefined>();
    expectTypeOf<Extract<AssistantEvent, { type: "answer" }>["answer"]["citations"]>().toEqualTypeOf<
      AssistantCitation[]
    >();
  });
});
