// The minimum server version: the SemVer comparison, the one warning line,
// and the SessionStart hook printing it only for an older server.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { isOlder, parseVersion, pluginManifest, serverVersionLine } from "../lib/server-version.mjs";

const HOOK = fileURLToPath(new URL("../hooks/loom-session-start.mjs", import.meta.url));
const MANIFEST = { version: "2.0.0", minServerVersion: "1.2.0" };

test("parseVersion reads a SemVer core and drops a prerelease or build suffix", () => {
  assert.deepEqual(parseVersion("1.2.3"), [1, 2, 3]);
  assert.deepEqual(parseVersion("v1.2.3"), [1, 2, 3]);
  assert.deepEqual(parseVersion("1.2.0-rc.1"), [1, 2, 0]);
  assert.deepEqual(parseVersion("1.2.0+abc.5"), [1, 2, 0]);
  assert.deepEqual(parseVersion("10.20.30-beta+exp.sha.5114f85"), [10, 20, 30]);
  for (const bad of ["", "1.2", "1.2.x", "latest", "1.2.3.4", null, undefined, 7, "1.2.3-"]) {
    assert.equal(parseVersion(bad), null, String(bad));
  }
});

test("isOlder compares numerically, and is false whenever either side does not parse", () => {
  assert.equal(isOlder("1.1.9", "1.2.0"), true);
  assert.equal(isOlder("0.9.0", "1.0.0"), true);
  assert.equal(isOlder("1.9.0", "1.10.0"), true);
  assert.equal(isOlder("1.2.0", "1.2.0"), false);
  assert.equal(isOlder("1.2.0-rc.1", "1.2.0"), false);
  assert.equal(isOlder("1.10.0", "1.9.0"), false);
  assert.equal(isOlder("2.0.0", "1.2.0"), false);
  assert.equal(isOlder("unknown", "1.2.0"), false);
  assert.equal(isOlder("1.0.0", ""), false);
});

test("serverVersionLine: one loom: line naming both versions for an older server, else nothing", () => {
  const line = serverVersionLine({ version: "1.1.0" }, { serverUrl: "https://loom.example", manifest: MANIFEST });
  assert.equal(
    line,
    "loom: the Loom server at https://loom.example runs 1.1.0, older than 1.2.0, " +
      "the oldest this plugin (2.0.0) supports; upgrade the server.",
  );
  assert.equal(line.split("\n").length, 1);
  for (const body of [{ version: "1.2.0" }, { version: "3.0.0" }, { version: "dev" }, { version: 1 }, {}, null]) {
    assert.equal(
      serverVersionLine(body, { serverUrl: "https://loom.example", manifest: MANIFEST }),
      "",
      JSON.stringify(body),
    );
  }
  assert.equal(
    serverVersionLine(
      { version: "0.1.0" },
      { serverUrl: "https://loom.example", manifest: { version: "2.0.0", minServerVersion: "" } },
    ),
    "",
  );
});

test("pluginManifest reads this plugin's own manifest", () => {
  const m = JSON.parse(fs.readFileSync(new URL("../.claude-plugin/plugin.json", import.meta.url), "utf8"));
  assert.deepEqual(pluginManifest(), { version: m.version, minServerVersion: m.minServerVersion });
  assert.ok(parseVersion(m.minServerVersion), m.minServerVersion);
  assert.deepEqual(pluginManifest("file:///nonexistent/lib/x.mjs"), { version: "", minServerVersion: "" });
});

/** A fake Loom whose /v1/health answers `health` (`hang` never answers it). */
async function fakeLoom(health) {
  const sockets = new Set();
  const srv = http.createServer((req, res) => {
    if (req.url.startsWith("/v1/health")) {
      if (health === "hang") return;
      res.writeHead(200, { "content-type": "application/json" });
      res.end(typeof health === "string" ? health : JSON.stringify(health));
      return;
    }
    req.resume();
    req.on("end", () => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  srv.on("connection", (s) => {
    sockets.add(s);
    s.on("close", () => sockets.delete(s));
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const close = () => {
    for (const s of sockets) s.destroy();
    srv.close();
  };
  return { close, url: `http://127.0.0.1:${srv.address().port}` };
}

function session(url, extraEnv = {}) {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-minver-")));
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(process.execPath, [HOOK, "SessionStart"], {
      cwd: dir,
      env: {
        PATH: process.env.PATH,
        HOME: dir,
        TMPDIR: dir,
        LOOM_BASE_URL: url,
        LOOM_API_KEY: "a.b.c",
        LOOM_CODE_MAP: "false",
        ...extraEnv,
      },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.on("close", (code) => {
      fs.rmSync(dir, { recursive: true, force: true });
      resolve({ code, stdout, stderr, ms: Date.now() - started });
    });
    child.stdin.end("{}");
  });
}

const minimum = pluginManifest().minServerVersion;

test("SessionStart warns once about a server older than the manifest's minimum", async () => {
  const [major, minor, patch] = parseVersion(minimum);
  // One below the minimum, at whichever position is above zero.
  const older =
    patch > 0 ? `${major}.${minor}.${patch - 1}` : minor > 0 ? `${major}.${minor - 1}.9` : `${major - 1}.9.9`;
  const loom = await fakeLoom({ version: older });
  try {
    const out = await session(loom.url);
    assert.equal(out.code, 0);
    assert.equal(out.stderr, "");
    const lines = out.stdout.split("\n").filter((l) => l.includes("upgrade the server"));
    assert.equal(lines.length, 1, out.stdout);
    assert.ok(lines[0].startsWith("loom: "), lines[0]);
    assert.ok(lines[0].includes(older) && lines[0].includes(minimum), lines[0]);
  } finally {
    loom.close();
  }
});

test("SessionStart says nothing about versions for a current, unversioned, non-SemVer or hung health", async () => {
  for (const health of [{ version: minimum }, {}, { version: "nightly" }, "not json", "hang"]) {
    const loom = await fakeLoom(health);
    try {
      const out = await session(loom.url);
      assert.equal(out.code, 0, JSON.stringify(health));
      assert.equal(out.stderr, "");
      assert.equal(out.stdout.includes("upgrade the server"), false, out.stdout);
      assert.ok(out.stdout.length > 0, "the primer still renders");
    } finally {
      loom.close();
    }
  }
  // Nothing listens at all.
  const out = await session("http://127.0.0.1:9");
  assert.equal(out.code, 0);
  assert.equal(out.stdout.includes("upgrade the server"), false);
});

test("a lower LOOM_HOOK_TIMEOUT_MS bounds the health call SessionStart waits for", async () => {
  const loom = await fakeLoom("hang");
  try {
    const out = await session(loom.url, { LOOM_HOOK_TIMEOUT_MS: "500" });
    assert.equal(out.code, 0);
    assert.ok(out.ms < 2500, `SessionStart took ${out.ms} ms against a hung health call`);
  } finally {
    loom.close();
  }
});

test("the Codex copy reads its own manifest", () => {
  const codexLib = new URL("../../codex-plugin/lib/server-version.mjs", import.meta.url);
  const manifest = JSON.parse(
    fs.readFileSync(new URL("../../codex-plugin/.codex-plugin/plugin.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(pluginManifest(codexLib), {
    version: manifest.version,
    minServerVersion: manifest.minServerVersion,
  });
});
