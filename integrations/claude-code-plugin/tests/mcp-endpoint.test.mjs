import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { claudeEndpoint, EndpointError, parseEndpoint } from "../lib/mcp-endpoint.mjs";
import { postBoundedResult, resolveServerUrl } from "../lib/loom-client.mjs";

test("the endpoint keeps a proxy base and the server's union spelling", () => {
  for (const query of ["coding+tracker", "coding%2Btracker", "Coding%20Tracker", "coding,tracker,coding"]) {
    const endpoint = parseEndpoint(`https://loom.example/proxy/mcp?toolset=${query}`);
    assert.equal(endpoint.base, "https://loom.example/proxy");
    assert.equal(endpoint.toolset, "coding+tracker");
  }
  assert.equal(parseEndpoint("http://[::1]:7777/mcp?toolset=readonly").base, "http://[::1]:7777");
});

test("the server selects absent, empty and unknown rosters", () => {
  for (const suffix of ["", "?toolset=", "?toolset=unknown", "?toolset=readonly+coding"]) {
    assert.equal(parseEndpoint("https://loom.example/mcp" + suffix).toolset, null);
  }
});

test("invalid endpoints yield an actionable notice without their credentials", () => {
  for (const value of [
    "",
    "https://loom.example",
    "https://loom.example/prefix",
    "ftp://loom.example/mcp",
    "https://secret:password@loom.example/mcp",
    "https://loom.example/mcp#secret",
    "https://loom.example/mcp?toolset=coding&toolset=readonly",
    "https://loom.example/mcp?token=secret&token=other",
    "https://loom.example/a%2fmcp",
    "https://loom.example/a\\mcp",
  ]) {
    assert.throws(
      () => parseEndpoint(value),
      (error) =>
        error instanceof EndpointError &&
        error.message.includes("full MCP URL") &&
        !error.message.includes("secret") &&
        !error.message.includes("password"),
    );
  }
});

test("an explicitly empty endpoint does not select another destination", () => {
  assert.throws(
    () => claudeEndpoint({ CLAUDE_PLUGIN_OPTION_SERVER_URL: "", LOOM_BASE_URL: "https://other" }),
    EndpointError,
  );
});

test("the installed endpoint wins over the environment's default base", () => {
  assert.equal(
    claudeEndpoint({
      CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://opt/proxy/mcp?toolset=tracker",
      LOOM_BASE_URL: "https://env",
    }).base,
    "https://opt/proxy",
  );
  assert.equal(claudeEndpoint({ CLAUDE_CODE_MCP_SERVER_URL: "https://helper/mcp" }).base, "https://helper");
});

test("the environment default preserves the manifest's literal proxy path", () => {
  for (const base of ["https://env/proxy", "https://env/proxy/", "https://env/proxy//"]) {
    const endpoint = claudeEndpoint({ LOOM_BASE_URL: base });
    assert.equal(endpoint.url, `${base}/mcp?toolset=coding`);
    assert.equal(endpoint.base, base);
  }
});

test("an accepted endpoint can send to its own derived base without changing the request path", async (t) => {
  const previous = {
    LOOM_CLIENT: process.env.LOOM_CLIENT,
    CLAUDE_PLUGIN_OPTION_SERVER_URL: process.env.CLAUDE_PLUGIN_OPTION_SERVER_URL,
  };
  t.after(() => {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });
  process.env.LOOM_CLIENT = "claude-code";
  for (const [endpoint, base, expectedUrl] of [
    [
      "https://loom.example/proxy/mcp?toolset=coding",
      "https://loom.example/proxy",
      "https://loom.example/proxy/v1/context.pack",
    ],
    [
      "https://loom.example/proxy//mcp?toolset=coding",
      "https://loom.example/proxy/",
      "https://loom.example/proxy//v1/context.pack",
    ],
    [
      "https://loom.example/proxy///mcp?toolset=coding",
      "https://loom.example/proxy//",
      "https://loom.example/proxy///v1/context.pack",
    ],
  ]) {
    await t.test(endpoint, async (t) => {
      process.env.CLAUDE_PLUGIN_OPTION_SERVER_URL = endpoint;
      const mockedFetch = t.mock.method(globalThis, "fetch", async () => ({
        status: 200,
        json: async () => ({ rendered: "primer" }),
      }));
      assert.equal(parseEndpoint(endpoint).base, base);
      assert.equal(resolveServerUrl(), base);
      assert.deepEqual(
        await postBoundedResult(
          "context.pack",
          { query: "checkout" },
          { token: "dummy", serverUrl: base, timeoutMs: 100 },
        ),
        { status: 200, body: { rendered: "primer" } },
      );
      assert.equal(mockedFetch.mock.callCount(), 1);
      const [url, options] = mockedFetch.mock.calls[0].arguments;
      assert.equal(url, expectedUrl);
      assert.equal(options.headers.authorization, "Bearer dummy");
      assert.deepEqual(JSON.parse(options.body), { query: "checkout" });
    });
  }
});

test("an endpoint with a trailing-slash base still rejects other destinations before fetch", async (t) => {
  const previous = {
    LOOM_CLIENT: process.env.LOOM_CLIENT,
    CLAUDE_PLUGIN_OPTION_SERVER_URL: process.env.CLAUDE_PLUGIN_OPTION_SERVER_URL,
  };
  t.after(() => {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });
  process.env.LOOM_CLIENT = "claude-code";
  process.env.CLAUDE_PLUGIN_OPTION_SERVER_URL = "https://loom.example/proxy//mcp?toolset=coding";
  const mockedFetch = t.mock.method(globalThis, "fetch", async () => ({ status: 200, json: async () => ({}) }));
  for (const serverUrl of [
    "https://other.example/proxy/",
    "https://loom.example/other/",
    "https://loom.example/proxy/other/",
    "https://loom.example/proxy",
    "https://loom.example/proxy//",
  ]) {
    await assert.rejects(
      postBoundedResult("context.pack", {}, { token: "dummy", serverUrl, timeoutMs: 100 }),
      EndpointError,
    );
  }
  assert.equal(mockedFetch.mock.callCount(), 0);
});
