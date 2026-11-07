// A user-scope entry duplicating the plugin's mount is named
// in the primer once a day.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { duplicateEntries, duplicateNotice, NOTICE_EVERY_MS } from "../lib/mcp-duplicate.mjs";

const URL_ = "https://loom.example";

test("duplicateEntries: same scheme+host, any path or toolset; other servers ignored", () => {
  const cfg = {
    mcpServers: {
      loom: { type: "http", url: "https://loom.example/mcp" },
      all: { type: "http", url: "https://LOOM.example/mcp?toolset=all" },
      other: { type: "http", url: "https://other.example/mcp" },
    },
  };
  assert.deepEqual(duplicateEntries(cfg, URL_), ["loom", "all"]);
  assert.deepEqual(duplicateEntries({}, URL_), []);
  assert.deepEqual(duplicateEntries(cfg, ""), []);
});

test("duplicateNotice: once per day, silent without a duplicate or a config", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "loom-dup-"));
  assert.equal(duplicateNotice({ serverUrl: URL_, home }), "", "no ~/.claude.json → silent");
  fs.writeFileSync(path.join(home, ".claude.json"), JSON.stringify({ mcpServers: { loom: { url: `${URL_}/mcp` } } }));
  let t = 1_000_000;
  const first = duplicateNotice({ serverUrl: URL_, home, now: () => t });
  assert.match(first, /registered twice/);
  assert.match(first, /claude mcp remove loom -s user/);
  t += 3600 * 1000;
  assert.equal(duplicateNotice({ serverUrl: URL_, home, now: () => t }), "", "within a day → silent");
  t += NOTICE_EVERY_MS;
  assert.match(duplicateNotice({ serverUrl: URL_, home, now: () => t }), /registered twice/);
});
