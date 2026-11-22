import assert from "node:assert/strict";
import { test } from "node:test";
import { LoomClient } from "../../sdk/typescript/dist/index.mjs";
import { createTools, invocationFetch, parseConfig } from "../../sdk/typescript/openclaw/tools.mjs";

const config = parseConfig({
  baseUrl: "https://loom.example.org",
  tokenEnv: "LOOM_TEST_TOKEN",
  org: "fixture-org",
  namespace: "fixture-notes",
  agentId: "main",
  userId: "fixture-user",
  sessionKey: "agent:main:main",
});

async function observeRevocation(mode, wrap = invocationFetch) {
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
        fetch: wrap(assertCurrent, async () => {
          sends++;
          if (mode === "pacing" && sends === 1) {
            return new Response("", { status: 429, headers: { "Retry-After": "0.01" } });
          }
          await Promise.resolve();
          current = false;
          return new Response(JSON.stringify({ abstain: false, results: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }),
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
  return { sends, waits };
}

for (const mode of ["pacing", "response"]) {
  test(`real SDK refuses authority revoked during ${mode}`, async () => {
    assert.deepEqual(await observeRevocation(mode), { sends: 1, waits: mode === "pacing" ? 1 : 0 });
  });
}

test("removing the production request guard permits a forbidden second send", async () => {
  // Mutation control: the response guard still refuses, but cannot undo the extra request.
  assert.deepEqual(await observeRevocation("pacing", (_assertCurrent, send) => send), { sends: 2, waits: 1 });
});
