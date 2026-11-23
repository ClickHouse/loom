import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";

it("the packed SDK installs without peers and makes requests from ESM and CommonJS", () => {
  const consumer = mkdtempSync(join(tmpdir(), "loom-sdk-consumer-"));
  const root = join(__dirname, "..", "..");
  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  const npm = (args: string[], cwd: string) =>
    execFileSync(process.execPath, [process.env.npm_execpath as string, ...args], {
      cwd,
      encoding: "utf8",
      timeout: 30_000,
    });
  try {
    const [packed] = JSON.parse(npm(["pack", "--json", "--pack-destination", consumer], root));
    writeFileSync(join(consumer, "package.json"), JSON.stringify({ private: true }));
    npm(
      [
        "install",
        join(consumer, packed.filename),
        "--offline",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--engine-strict",
        "--omit=peer",
      ],
      consumer,
    );
    const installed = JSON.parse(readFileSync(join(consumer, "node_modules/@clickhouse/loom/package.json"), "utf8"));
    expect(installed.engines).toEqual(pkg.engines);
    for (const [extension, load] of [
      ["mjs", 'import { LoomClient, SDK_VERSION } from "@clickhouse/loom"; import assert from "node:assert/strict";'],
      [
        "cjs",
        'const { LoomClient, SDK_VERSION } = require("@clickhouse/loom"); const assert = require("node:assert/strict");',
      ],
    ]) {
      const script = join(consumer, `consumer.${extension}`);
      writeFileSync(
        script,
        `${load}
        async function main() {
          const client = new LoomClient({
            baseUrl: "https://loom.example.com", apiKey: "consumer-test",
            fetch: async (url, init) => {
              assert.equal(url, "https://loom.example.com/v1/memory.search");
              assert.deepEqual(JSON.parse(init.body), { org: "o", namespace: "n", query: "q" });
              return new Response(JSON.stringify({ results: [] }));
            },
          });
          assert.deepEqual(await client.memory.search({ org: "o", namespace: "n", query: "q" }), { results: [] });
          assert.equal(SDK_VERSION, ${JSON.stringify(pkg.version)});
        }
        main().catch(error => { console.error(error); process.exitCode = 1; });
        `,
      );
      execFileSync(process.execPath, [script], { cwd: consumer, timeout: 10_000 });
    }
  } finally {
    rmSync(consumer, { recursive: true, force: true });
  }
}, 90_000);
