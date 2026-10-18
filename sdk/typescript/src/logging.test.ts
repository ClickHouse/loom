/** One redaction point; the key never reaches the sink at any level. */
import { describe, expect, it } from "vitest";
import type { Logger } from "./logging.js";
import { parseLogLevel, redactHeaders, redactHeaderValue, withLevel } from "./logging.js";
import { Transport } from "./transport.js";

const KEY = "sk-loom-very-secret-1234";

function capture(): { logger: Logger; lines: Array<[string, string]> } {
  const lines: Array<[string, string]> = [];
  const push =
    (level: string) =>
    (m: string, ...args: unknown[]) => {
      lines.push([level, [m, ...args.map((a) => JSON.stringify(a))].join(" ")]);
    };
  return { logger: { debug: push("debug"), info: push("info"), warn: push("warn"), error: push("error") }, lines };
}

describe("redaction", () => {
  it("keeps the scheme and the last four characters of a credential header only", () => {
    expect(redactHeaderValue("Authorization", `Bearer ${KEY}`)).toBe("Bearer …1234");
    expect(redactHeaderValue("authorization", "Bearer short")).toBe("Bearer …");
    expect(redactHeaderValue("X-Api-Key", KEY)).toBe("…1234");
    expect(redactHeaderValue("Cookie", "session=abcdefghijkl")).toBe("…ijkl");
    expect(redactHeaderValue("X-Request-Id", "rid-1")).toBe("rid-1");
    expect(redactHeaders({ Authorization: `Bearer ${KEY}`, Accept: "application/json" })).toEqual({
      Authorization: "Bearer …1234",
      Accept: "application/json",
    });
    expect(redactHeaders(new Headers({ Authorization: `Bearer ${KEY}` }))).toEqual({ authorization: "Bearer …1234" });
  });

  it("parses a level and names the source of a bad one", () => {
    expect(parseLogLevel(" INFO ", "LOOM_LOG_LEVEL")).toBe("info");
    expect(parseLogLevel("CRITICAL", "LOOM_LOG_LEVEL")).toBe("error");
    expect(parseLogLevel("warning", "LOOM_LOG_LEVEL")).toBe("warn");
    expect(() => parseLogLevel("loud", "LOOM_LOG_LEVEL")).toThrow(/LOOM_LOG_LEVEL: invalid log level "loud"/);
  });

  it("filters by level", () => {
    const { logger, lines } = capture();
    const l = withLevel(logger, "warn");
    l.debug("a");
    l.info("b");
    l.warn("c");
    l.error("d");
    expect(lines.map(([lvl]) => lvl)).toEqual(["warn", "error"]);
  });
});

describe("Transport logging", () => {
  async function run(level: "debug" | "info" | "warn" | "error") {
    const { logger, lines } = capture();
    const script = [
      { status: 503, body: { detail: { code: "OVERLOADED", message: "busy" } } },
      { status: 200, body: { ok: 1 }, headers: { "X-Request-Id": "rid-7" } },
      { status: 404, body: { detail: { code: "RESOURCE_NOT_FOUND", message: "no" } } },
    ];
    const fetch = async (): Promise<Response> => {
      const s = script.shift();
      if (!s) throw new Error("unscripted");
      return new Response(JSON.stringify(s.body), { status: s.status, headers: s.headers ?? {} });
    };
    const t = new Transport({
      baseUrl: "http://x",
      apiKey: KEY,
      fetch,
      sleep: async () => {},
      logger,
      logLevel: level,
      retry: { backoffMs: 1 },
    });
    await t.post("memory.get", { org: "o" });
    await t.post("memory.get", { org: "o" }).catch(() => undefined);
    return lines;
  }

  it("never logs the raw API key at any level", async () => {
    for (const level of ["debug", "info", "warn", "error"] as const) {
      const lines = await run(level);
      expect(lines.length).toBeGreaterThan(0);
      for (const [, text] of lines) expect(text).not.toContain(KEY);
    }
  });

  it("writes one summary line per call at info, retries at warn, the failure at error, headers redacted at debug", async () => {
    const lines = await run("debug");
    const info = lines.filter(([l]) => l === "info").map(([, t]) => t);
    expect(info).toHaveLength(1);
    expect(info[0]).toMatch(/^memory\.get 200 \d+ ms attempts=2 rid=rid-7$/);
    const warn = lines.filter(([l]) => l === "warn").map(([, t]) => t);
    expect(warn[0]).toMatch(/^memory\.get attempt 1\/3 503 OVERLOADED; retrying in \d+ ms \(rid=/);
    const error = lines.filter(([l]) => l === "error").map(([, t]) => t);
    expect(error[0]).toMatch(/^memory\.get failed after \d+ ms: NotFound RESOURCE_NOT_FOUND \(404\) \(rid=/);
    const debug = lines.filter(([l]) => l === "debug").map(([, t]) => t);
    expect(debug[0]).toContain("Bearer …1234");
    expect(debug[0]).toMatch(/→ POST http:\/\/x\/v1\/memory\.get attempt 1 waits \d+ of 60000 ms bytes \d+/);
  });

  it("logLevel off writes nothing; the default level is warn", async () => {
    const { logger, lines } = capture();
    const fetch = async (): Promise<Response> => new Response("{}", { status: 200 });
    const t = new Transport({ baseUrl: "http://x", fetch, logger, logLevel: "off" });
    await t.post("memory.get", {});
    expect(lines).toEqual([]);
    const t2 = new Transport({ baseUrl: "http://x", fetch, logger });
    await t2.post("memory.get", {});
    expect(lines).toEqual([]); // info is below the default warn
  });
});

describe("LOOM_LOG_LEVEL", () => {
  it("an unreadable value warns and keeps the default instead of failing the client", () => {
    const env = (globalThis as { process: { env: Record<string, string | undefined> } }).process.env;
    const saved = env.LOOM_LOG_LEVEL;
    const warned: string[] = [];
    const sink = { debug() {}, info() {}, warn: (m: string) => warned.push(m), error() {} };
    try {
      env.LOOM_LOG_LEVEL = "loud";
      expect(() => new Transport({ baseUrl: "http://x", logger: sink })).not.toThrow();
      expect(warned).toEqual([
        'LOOM_LOG_LEVEL: invalid log level "loud"; expected one of debug, info, warn, error, off; using warn',
      ]);
    } finally {
      if (saved === undefined) delete env.LOOM_LOG_LEVEL;
      else env.LOOM_LOG_LEVEL = saved;
    }
  });
});
