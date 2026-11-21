import { inspect } from "node:util";
import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { AuthError } from "./errors.js";
import { type FetchLike, Transport } from "./transport.js";

const parameters = {
  numRuns: 100,
  seed: Number(process.env.LOOM_FUZZ_SEED ?? 0x6c6f6f6d),
  path: process.env.LOOM_FUZZ_PATH,
};

const token = fc
  .array(fc.constantFrom(..."abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"), { maxLength: 48 })
  .map((letters) => `credential-${letters.join("")}-fixture`);

const baseUrl = fc
  .record({
    scheme: fc.constantFrom("http", "https"),
    host: fc.constantFrom("LOOM.TEST", "loom.test", "xn--bcher-kva.test", "127.0.0.1", "[::1]"),
    port: fc.constantFrom("", ":80", ":443", ":7777"),
    path: fc.array(fc.constantFrom("api", "%2f", "%5c", "\u2603"), { maxLength: 4 }),
  })
  .map(({ scheme, host, port, path }) => `${scheme}://${host}${port}/${path.join("/")}`);

function recorder() {
  const calls: Array<{ url: string; headers: Record<string, string> }> = [];
  const fetch: FetchLike = async (url, init) => {
    calls.push({ url, headers: { ...(init.headers as Record<string, string>) } });
    return new Response("{}", { status: 200 });
  };
  return { calls, fetch };
}

function capturedLogger() {
  const logs: string[] = [];
  const log = (...values: unknown[]) => {
    logs.push(inspect(values, { depth: null, customInspect: false }));
  };
  return { logs, logger: { debug: log, info: log, warn: log, error: log } };
}

function expectPrivateOutput(outputs: string[], secret: string) {
  for (const output of outputs) expect(output).not.toContain(secret);
}

describe("Transport properties", () => {
  it("detects nested log credentials before the supplied object is changed", () => {
    const { logs, logger } = capturedLogger();
    const secret = "credential-leak-control-fixture";
    const headers = { Authorization: `Bearer ${secret}` };
    logger.debug("request", { context: { headers } });
    headers.Authorization = "redacted-later";

    expect(logs).toHaveLength(1);
    expect(logs[0]).not.toContain("redacted-later");
    expect(() => expectPrivateOutput(logs, secret)).toThrow();
  });

  it("binds provider tokens to the canonical server origin before reading them", async () => {
    await fc.assert(
      fc.asyncProperty(
        baseUrl,
        token,
        fc.constantFrom("protocol", "hostname", "port"),
        async (url, secret, changed) => {
          const origin = new URL(url).origin;
          const other = new URL(origin);
          if (changed === "protocol") other.protocol = other.protocol === "http:" ? "https:" : "http:";
          else if (changed === "hostname") other.hostname = "other.loom.test";
          else other.port = other.port === "444" ? "445" : "444";
          expect(other.origin).not.toBe(origin);

          for (const allowed of [true, false]) {
            const { calls, fetch } = recorder();
            let reads = 0;
            const transport = new Transport({
              baseUrl: url,
              tokenProvider: {
                origin: allowed ? origin : other.origin,
                token: () => {
                  reads++;
                  return secret;
                },
              },
              fetch,
              retry: { maxAttempts: 1 },
              logLevel: "off",
            });
            if (allowed) {
              await expect(transport.post("memory.get", {})).resolves.toEqual({});
              expect(reads).toBe(1);
              expect(calls).toHaveLength(1);
              expect(new URL(calls[0].url).origin).toBe(origin);
              expect(calls[0].headers.Authorization).toBe(`Bearer ${secret}`);
            } else {
              await expect(transport.post("memory.get", {})).rejects.toBeInstanceOf(AuthError);
              expect(reads).toBe(0);
              expect(calls).toHaveLength(0);
            }
          }
        },
      ),
      parameters,
    );
  });

  it("sends one selected credential while inspection and logs keep it private", async () => {
    const authorization = fc
      .array(fc.boolean(), { minLength: 13, maxLength: 13 })
      .map((upper) => Array.from("authorization", (letter, i) => (upper[i] ? letter.toUpperCase() : letter)).join(""));
    await fc.assert(
      fc.asyncProperty(token, authorization, async (secret, header) => {
        const { calls, fetch } = recorder();
        const { logs, logger } = capturedLogger();
        const transport = new Transport({
          baseUrl: "https://loom.test",
          apiKey: `  ${secret}  `,
          headers: { Authorization: "caller-supplied", [header]: "caller-supplied" },
          fetch,
          retry: { maxAttempts: 1 },
          logger,
          logLevel: "debug",
        });
        await transport.post("memory.get", {});
        expect(calls).toHaveLength(1);
        const credentials = Object.entries(calls[0].headers).filter(([name]) => name.toLowerCase() === "authorization");
        expect(credentials).toEqual([["Authorization", `Bearer ${secret}`]]);
        const outputs = [JSON.stringify(transport), inspect(transport), ...logs];
        expectPrivateOutput(outputs, secret);
        expectPrivateOutput(outputs, "caller-supplied");
      }),
      parameters,
    );
  });

  it("keeps request IDs within the wire header boundary", async () => {
    const validId = fc
      .array(fc.constantFrom(..."abcABC012._-"), { minLength: 1, maxLength: 64 })
      .map((parts) => parts.join(""));
    const requestId = fc.oneof(
      fc.string({ maxLength: 256 }),
      validId,
      fc.tuple(validId, fc.constantFrom("\r", "\n", "\u2028", "\u2029")).map(([id, ending]) => id + ending),
      fc.array(fc.integer({ min: 0, max: 0x10ffff }), { maxLength: 48 }).map((parts) => String.fromCodePoint(...parts)),
      fc.constantFrom("", "\r", "\n", "\u0000", "x".repeat(65)),
    );
    await fc.assert(
      fc.asyncProperty(requestId, async (supplied) => {
        const { calls, fetch } = recorder();
        const transport = new Transport({
          baseUrl: "https://loom.test",
          fetch,
          retry: { maxAttempts: 1 },
          logLevel: "off",
        });
        await transport.post("memory.get", {}, { requestId: supplied });
        expect(calls).toHaveLength(1);
        const sent = calls[0].headers["X-Request-Id"];
        expect(sent.length).toBeGreaterThan(0);
        expect(sent.length).toBeLessThanOrEqual(64);
        expect(sent).not.toMatch(/[^A-Za-z0-9._-]/);
        expect(new Headers(calls[0].headers).get("X-Request-Id")).toBe(sent);
        if (supplied.length > 0 && supplied.length <= 64 && !/[^A-Za-z0-9._-]/.test(supplied))
          expect(sent).toBe(supplied);
        else expect(sent).not.toBe(supplied);
      }),
      parameters,
    );
  });
});
