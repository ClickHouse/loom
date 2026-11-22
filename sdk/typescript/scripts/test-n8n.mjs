import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// n8n's Zod 3 host and the other frameworks' Zod 4 host need separate installs.
// Test the published artifact, so development dependencies cannot hide a missing file.
const workspace = mkdtempSync(join(tmpdir(), "loom-n8n-package-"));
try {
  const consumer = join(workspace, "consumer");
  mkdirSync(consumer);
  const npm = (args, cwd = consumer) =>
    execFileSync("npm", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] });
  const [packed] = JSON.parse(
    npm(["pack", "--ignore-scripts", "--json", "--pack-destination", workspace], process.cwd()),
  );
  const tarball = join(workspace, packed.filename);
  const install = () =>
    npm(["install", "--no-save", "--package-lock=false", "--ignore-scripts", "--no-audit", "--no-fund", tarball]);
  install();
  const installed = join(consumer, "node_modules/@clickhouse/loom");
  const metadata = JSON.parse(readFileSync(join(installed, "package.json"), "utf8"));
  assert.equal(metadata.name, "@clickhouse/loom");
  assert.deepEqual(metadata.dependencies ?? {}, {});
  execFileSync(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      `
  import assert from 'node:assert/strict';
  import { createRequire } from 'node:module';
  import { Agent } from '@clickhouse/loom';
  const require = createRequire(import.meta.url);
  assert.equal(typeof Agent, 'function');
  assert.equal(typeof require('@clickhouse/loom').Agent, 'function');
  assert.throws(() => require.resolve('n8n-workflow'), { code: 'MODULE_NOT_FOUND' });
`,
    ],
    { cwd: consumer, stdio: "inherit" },
  );

  for (const file of ["package.json", "package-lock.json"]) {
    copyFileSync(resolve("test/n8n/host", file), join(consumer, file));
  }
  npm(["ci", "--ignore-scripts", "--no-audit", "--no-fund"]);
  install();
  const tests = ["nodes.test.cjs", "package.test.cjs", ...(process.argv.includes("--live") ? ["live.test.cjs"] : [])];
  for (const file of tests) copyFileSync(resolve("test/n8n", file), join(consumer, file));
  console.log(`Testing the shared npm tarball in ${consumer}`);
  execFileSync(process.execPath, ["--test", ...tests], { cwd: consumer, stdio: "inherit" });
} finally {
  rmSync(workspace, { recursive: true, force: true });
}
