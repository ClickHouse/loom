const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { dirname, join } = require("node:path");
const { test } = require("node:test");

const root = dirname(require.resolve("@clickhouse/loom/package.json"));
const pkg = require("@clickhouse/loom/package.json");

test("the shared package contains loadable native nodes, credentials and icons", () => {
  assert.equal(pkg.name, "@clickhouse/loom");
  assert.equal(pkg.n8n.nodes.length, 2);
  assert.equal(pkg.n8n.credentials.length, 1);
  for (const file of [...pkg.n8n.nodes, ...pkg.n8n.credentials]) {
    const Constructor = Object.values(require(join(root, file)))[0];
    const node = new Constructor();
    const configured = node.description?.icon ?? node.icon;
    const icon = typeof configured === "string" ? configured : configured.light;
    assert.match(readFileSync(join(root, dirname(file), icon.replace("file:", "")), "utf8"), /<svg/);
  }
  const metadata = JSON.parse(readFileSync(join(root, "dist/integrations/n8n/package.json"), "utf8"));
  assert.equal(metadata.type, "commonjs");
});

test("the shared adapter subpath loads in both module formats", async () => {
  const esm = await import("@clickhouse/loom/integrations/n8n");
  const cjs = require("@clickhouse/loom/integrations/n8n");
  for (const name of ["Loom", "LoomApi", "LoomChatHistory", "LoomMemory"]) {
    assert.equal(typeof esm[name], "function");
    assert.equal(typeof cjs[name], "function");
  }
});
