import { describe, expect, it } from "vitest";
import { SessionProtection, SessionProtectionUnavailable } from "./session-protection.js";
import { Transport } from "./transport.js";

const headers = { "content-type": "text/event-stream; charset=utf-8" };
const encode = (value: string) => new TextEncoder().encode(value);

function client(source: Response) {
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
    fetch: async () => source,
    logLevel: "off",
  });
  return { state, response: transport.stream("/assistant/chat", {}) };
}

function chunks(values: Uint8Array[]): Response {
  let index = 0;
  return new Response(
    new ReadableStream<Uint8Array>({
      pull(controller) {
        if (index === values.length) controller.close();
        else controller.enqueue(values[index++]);
      },
    }),
    { headers },
  );
}

describe("protected event framing", () => {
  it.each(["\n", "\r\n", "\r"])("accepts byte-split %j frames and preserves every byte", async (newline) => {
    const wire = `\ufeff: comment${newline}${newline}data: {"text":"保護",${newline}data: "mirrored":true}${newline}${newline}data: [DONE]${newline}${newline}`;
    const bytes = encode(wire);
    const { state, response } = client(chunks(Array.from(bytes, (byte) => Uint8Array.of(byte))));
    expect(new Uint8Array(await (await response).arrayBuffer())).toEqual(bytes);
    expect(state.value).toBe("protected");
  });

  it.each([
    Uint8Array.of(100, 97, 116, 97, 58, 32, 0xff, 10, 10),
    encode(`data: ${" ".repeat(4 * 1024 * 1024)}`),
    encode('data: {"text":"unfinished"}'),
  ])("withholds invalid UTF-8, oversized or unfinished frames (%#)", async (bytes) => {
    const { state, response } = client(chunks([bytes]));
    await expect(response.then((value) => value.arrayBuffer())).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(state.value).toBe("protected");
  });

  it.each([
    new Response("data: {}\n\n", { headers: { "content-type": "application/x-text/event-stream-copy" } }),
    new Response("ordinary JSON", { headers: { "content-type": "application/json" } }),
    new Response("data: {}\n\n", { headers: { ...headers, "content-encoding": "gzip" } }),
    new Response(null, { status: 204, headers }),
  ])("refuses unverifiable successful bodies before exposing a response (%#)", async (source) => {
    const { state, response } = client(source);
    await expect(response).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(state.value).toBe("protected");
  });

  it("retains response metadata through clones with guarded internal bodies", async () => {
    const wire = 'data: {"text":"ordinary"}\n\n';
    const source = new Response(wire, {
      status: 201,
      statusText: "Created",
      headers: { ...headers, "x-test": "retained" },
    });
    Object.defineProperties(source, {
      url: { value: "https://inert.invalid/redirected" },
      redirected: { value: true },
      type: { value: "cors" },
    });
    const { state, response } = client(source);
    const actual = await response;
    const clone = actual.clone().clone();
    for (const value of [actual, clone]) {
      expect([
        value.status,
        value.statusText,
        value.url,
        value.redirected,
        value.type,
        value.headers.get("x-test"),
      ]).toEqual([201, "Created", source.url, true, "cors", "retained"]);
    }
    expect(await Response.prototype.text.call(clone)).toBe(wire);
    expect(await actual.text()).toBe(wire);
    expect(state.value).toBe("clean");
  });
});
