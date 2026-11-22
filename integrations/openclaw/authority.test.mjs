import assert from "node:assert/strict";
import { test } from "node:test";
import { LoomClient } from "../../sdk/typescript/dist/index.mjs";
import { createTools, parseConfig } from "../../sdk/typescript/openclaw/tools.mjs";

const config = parseConfig({
  baseUrl: "https://loom.example.org",
  tokenEnv: "LOOM_TEST_TOKEN",
  org: "fixture-org",
  namespace: "fixture-notes",
  agentId: "main",
  userId: "fixture-user",
  sessionKey: "agent:main:main",
});

for (const mode of ["pacing", "response"]) {
  test(`real SDK refuses authority revoked during ${mode}`, async () => {
    let current = true;
    let sends = 0;
    let waits = 0;
    const context = {
      agentId: "main",
      sessionKey: config.sessionKey,
      assertInvocationCurrent() {
        if (!current) throw new Error("invocation closed");
      },
    };
    const [, search] = createTools(
      config,
      context,
      (assertCurrent) =>
        new LoomClient({
          baseUrl: config.baseUrl,
          apiKey: "synthetic-token",
          logLevel: "off",
          async fetch() {
            assertCurrent();
            sends++;
            if (mode === "pacing") return new Response("", { status: 429, headers: { "Retry-After": "0.01" } });
            await Promise.resolve();
            current = false;
            return new Response(JSON.stringify({ abstain: false, results: [] }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          },
          async sleep() {
            waits++;
            await Promise.resolve();
            current = false;
          },
        }),
    );
    const abort = new AbortController();
    await assert.rejects(search.execute("fixture-call", { query: "editor" }, abort.signal));
    assert.equal(abort.signal.aborted, false);
    assert.equal(sends, 1);
    assert.equal(waits, mode === "pacing" ? 1 : 0);
  });
}
