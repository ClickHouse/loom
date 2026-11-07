// A plugin's shipped default server URL sits below every
// user-set source (env, install option, ~/.loom/config.json) and above localhost.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const lib = fileURLToPath(new URL("../lib/loom-client.mjs", import.meta.url));

function resolve(env, defaults) {
  const dir = mkdtempSync(join(tmpdir(), "loom-defaults-"));
  const cfg = join(dir, "config.json");
  if (env.CONFIG_URL) writeFileSync(cfg, JSON.stringify({ url: env.CONFIG_URL }));
  const def = join(dir, "plugin-defaults.json");
  if (defaults) writeFileSync(def, JSON.stringify(defaults));
  const script = `import { pluginDefaults, resolveServerUrl } from ${JSON.stringify(lib)};
    pluginDefaults(${JSON.stringify(def)}); console.log(resolveServerUrl());`;
  const clean = { PATH: process.env.PATH, HOME: dir, LOOM_CONFIG_FILE: cfg, LOOM_HOOK_NO_MAIN: "1" };
  if (env.LOOM_BASE_URL) clean.LOOM_BASE_URL = env.LOOM_BASE_URL;
  return execFileSync(process.execPath, ["--input-type=module", "-e", script], { env: clean }).toString().trim();
}

test("the shipped default answers when nothing user-set does", () => {
  assert.equal(resolve({}, { server_url: "https://loom.example/" }), "https://loom.example");
});

test("~/.loom/config.json and LOOM_BASE_URL both beat the shipped default", () => {
  assert.equal(
    resolve({ CONFIG_URL: "https://mine.example" }, { server_url: "https://loom.example" }),
    "https://mine.example",
  );
  assert.equal(
    resolve({ LOOM_BASE_URL: "https://env.example" }, { server_url: "https://loom.example" }),
    "https://env.example",
  );
});

test("no defaults file keeps the localhost fallback", () => {
  assert.equal(resolve({}, null), "http://localhost:7777");
});
