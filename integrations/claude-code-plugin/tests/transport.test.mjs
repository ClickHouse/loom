// The one request path: bounds, headers, and what callers see on failure.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { postBounded, postFireAndForget, requestJson } from "../lib/loom-client.mjs";

// No credential from this machine may reach the fake servers below.
process.env.LOOM_CONFIG_FILE = "/nonexistent/loom-config.json";
process.env.LOOM_API_KEY_FILE = "/nonexistent/loom-token";
delete process.env.LOOM_API_KEY;
delete process.env.CLAUDE_PLUGIN_OPTION_TOKEN;

async function server(handler) {
  const seen = [];
  const sockets = new Set();
  const srv = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      seen.push({ url: req.url, method: req.method, headers: req.headers, body });
      handler(req, res);
    });
  });
  srv.on("connection", (s) => {
    sockets.add(s);
    s.on("close", () => sockets.delete(s));
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const close = () =>
    new Promise((r) => {
      for (const s of sockets) s.destroy();
      srv.close(() => r());
    });
  return { url: `http://127.0.0.1:${srv.address().port}`, seen, close };
}

const json = (status, body) => (req, res) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};

test("postBounded returns the body of a 2xx answer and sends the bearer", async () => {
  const s = await server(json(200, { ok: 1 }));
  try {
    assert.deepEqual(await postBounded("x.y", { a: 1 }, { token: "t0k", serverUrl: s.url }), { ok: 1 });
    assert.equal(s.seen[0].url, "/v1/x.y");
    assert.equal(s.seen[0].headers.authorization, "Bearer t0k");
    assert.deepEqual(JSON.parse(s.seen[0].body), { a: 1 });
  } finally {
    await s.close();
  }
});

test("postBounded is null on an error status, a non-JSON 2xx, a refused connection and a timeout", async () => {
  const err = await server(json(401, { error: "no" }));
  const html = await server((req, res) => {
    res.writeHead(200, { "content-type": "text/html" });
    res.end("<html>proxy</html>");
  });
  const hang = await server(() => {});
  try {
    assert.equal(await postBounded("x", {}, { token: "t", serverUrl: err.url }), null);
    assert.equal(await postBounded("x", {}, { token: "t", serverUrl: html.url }), null);
    assert.equal(await postBounded("x", {}, { token: "t", serverUrl: "http://127.0.0.1:9" }), null);
    const t0 = Date.now();
    assert.equal(await postBounded("x", {}, { token: "t", serverUrl: hang.url, timeoutMs: 300 }), null);
    assert.ok(Date.now() - t0 < 2000, "the bound held");
  } finally {
    await Promise.all([err.close(), html.close(), hang.close()]);
  }
});

test("requestJson reports the status and a readable error, never throws", async () => {
  const r = await requestJson("http://127.0.0.1:9/v1/health", { method: "GET", timeoutMs: 1000 });
  assert.equal(r.status, 0);
  assert.equal(r.body, null);
  assert.equal(typeof r.error, "string");
  const bad = await requestJson("not a url", { timeoutMs: 1000 });
  assert.equal(bad.status, 0);
});

test("no credential means no request", async () => {
  const s = await server(json(200, {}));
  try {
    assert.equal(await postBounded("x", {}, { token: "", serverUrl: s.url }), null);
    postFireAndForget("x", {}, { token: "", serverUrl: s.url });
    await new Promise((r) => setTimeout(r, 200));
    assert.equal(s.seen.length, 0);
  } finally {
    await s.close();
  }
});

test("a fire-and-forget post carries a request id and returns before the answer", async () => {
  const s = await server(json(200, {}));
  try {
    assert.equal(postFireAndForget("obs.x", { n: 1 }, { token: "t", serverUrl: s.url }), undefined);
    for (let i = 0; i < 40 && !s.seen.length; i++) await new Promise((r) => setTimeout(r, 25));
    assert.equal(s.seen.length, 1);
    assert.match(s.seen[0].headers["x-request-id"], /^loomhook-\d+-[a-z0-9]+$/);
  } finally {
    await s.close();
  }
});
