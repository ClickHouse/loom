import { afterEach, describe, expect, it } from "vitest";
import { LoomClient } from "./client.js";
import { envVar, runtimeIdent, scriptName } from "./runtime.js";

const proc = (globalThis as { process: { env: Record<string, string | undefined>; argv: string[] } }).process;
const saved = { env: proc.env, argv: proc.argv };

afterEach(() => {
  proc.env = saved.env;
  proc.argv = saved.argv;
});

describe("runtime", () => {
  it("reads one environment variable, and nothing where the runtime refuses the read", () => {
    proc.env = { ...saved.env, LOOM_BASE_URL: "http://from-env" };
    expect(envVar("LOOM_BASE_URL")).toBe("http://from-env");
    // Deno without --allow-env throws on every read.
    proc.env = new Proxy({} as Record<string, string | undefined>, {
      get() {
        throw new Error("Requires env access");
      },
    });
    expect(envVar("LOOM_BASE_URL")).toBeUndefined();
    expect(() => new LoomClient({ baseUrl: "http://x", fetch: async () => new Response("{}") })).not.toThrow();
  });

  it("names the running script without its extension", () => {
    proc.argv = ["/usr/bin/node", "/srv/bots/support-bot.mjs"];
    expect(scriptName()).toBe("support-bot");
    proc.argv = ["node"];
    expect(scriptName()).toBeUndefined();
  });

  it("identifies the runtime without naming the host", () => {
    expect(runtimeIdent()).toMatch(/^node\/\d+\.\d+\.\d+ \w+ \w+$/);
  });
});
