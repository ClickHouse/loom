import "./temp-home.mjs";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { establishFixtureSession } from "./established-session.mjs";
import { hostEnv } from "./host-env.mjs";
import { protectSession, sessionIsProtected } from "../lib/session-protection.mjs";
import { tmpStatePath } from "../lib/tmp-state.mjs";

const HOOK = fileURLToPath(new URL("../hooks/loom-session-start.mjs", import.meta.url));
const TOKEN = "fixture.token";
const ORG = "fixture-org";
const NAMESPACE = "fixture-ns";
const ALLOWED = new Set([
  "/v1/health",
  "/v1/context.pack",
  "/v1/task.counts",
  "/v1/namespace.get",
  "/v1/collection.list",
  "/v1/session.brief_list",
]);

for (const mode of ["ordinary", "mirrored", "late-sibling-mark"]) {
  test(`SessionStart ${mode} checks its family before primer and cache publication`, async () => {
    const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-primer-protection-")));
    const home = path.join(directory, "home");
    const tmp = path.join(directory, "tmp");
    fs.mkdirSync(home, { mode: 0o700 });
    fs.mkdirSync(tmp, { mode: 0o700 });
    let server;
    let child;
    let completed;
    try {
      const sessionId = `primer-${mode}`;
      const root = establishFixtureSession(home, sessionId);
      const seen = [];
      const pending = [];
      let ready = false;
      server = http.createServer((req, res) => {
        req.resume();
        req.on("end", () => {
          const route = new URL(req.url, "http://fixture.invalid").pathname;
          seen.push(route);
          if (!ALLOWED.has(route)) {
            res.writeHead(500).end("{}");
            return;
          }
          const reply = () => {
            const body =
              route === "/v1/health"
                ? { version: "999.0.0" }
                : route === "/v1/context.pack"
                  ? { rendered: mode === "ordinary" ? "ORDINARY PACK RULE" : "PRIVATE PACK RULE" }
                  : route === "/v1/collection.list"
                    ? { collections: [] }
                    : route === "/v1/session.brief_list"
                      ? { briefs: [] }
                      : {};
            // Every automatic result marks before its own cache consumer.
            if (mode === "mirrored" && route !== "/v1/health") body.mirrored = true;
            res.writeHead(200, { "content-type": "application/json" });
            res.end(JSON.stringify(body));
          };
          pending.push(reply);
          if (route === "/v1/context.pack") {
            // No response is released before the actual pack lookup is pending.
            if (mode === "late-sibling-mark") protectSession(root);
            ready = true;
          }
          if (ready) for (const send of pending.splice(0)) send();
        });
      });
      await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
      const url = `http://127.0.0.1:${server.address().port}`;
      fs.writeFileSync(
        path.join(home, ".loom", "config.json"),
        JSON.stringify({
          token: TOKEN,
          url,
          org: ORG,
          namespace: NAMESPACE,
        }),
        { mode: 0o600 },
      );
      const tokenId = createHash("sha256").update(TOKEN).digest("hex").slice(0, 16);
      const caches = [
        tmpStatePath("taskq", `${url}|${ORG}|${NAMESPACE}`, { tmp }),
        tmpStatePath("collections", `${url}|${ORG}|${tokenId}|`, { tmp }),
      ];
      const before = caches.map((cache) => {
        fs.writeFileSync(cache, JSON.stringify({ ts: 0, line: "STALE CACHE" }), { mode: 0o600 });
        return fs.readFileSync(cache);
      });
      child = spawn(process.execPath, [HOOK, "SessionStart"], {
        cwd: home,
        env: hostEnv({
          PATH: process.env.PATH,
          HOME: home,
          TMPDIR: tmp,
          LOOM_BASE_URL: url,
          LOOM_API_KEY: TOKEN,
          LOOM_CODE_MAP: "false",
        }),
      });
      completed = new Promise((resolve, reject) => {
        let stdout = "";
        let stderr = "";
        child.stdout.on("data", (part) => {
          stdout += part;
        });
        child.stderr.on("data", (part) => {
          stderr += part;
        });
        child.once("error", reject);
        child.once("close", (code) => resolve({ code, stdout, stderr }));
      });
      child.stdin.end(
        JSON.stringify({ session_id: sessionId, cwd: home, hook_event_name: "SessionStart", source: "startup" }),
      );
      const answer = await completed;
      assert.equal(answer.code, 0, answer.stderr);
      assert.equal(answer.stderr, "");
      assert.equal(seen.filter((route) => route === "/v1/context.pack").length, 1);
      assert.deepEqual(
        seen.filter((route) => !ALLOWED.has(route)),
        [],
      );
      if (mode === "ordinary") {
        assert.match(answer.stdout, /ORDINARY PACK RULE/);
        assert.ok(seen.includes("/v1/task.counts"));
        assert.ok(seen.includes("/v1/collection.list"));
        for (let i = 0; i < caches.length; i++) assert.notDeepEqual(fs.readFileSync(caches[i]), before[i]);
        assert.equal(sessionIsProtected(root), false);
      } else {
        assert.equal(answer.stdout, "");
        for (let i = 0; i < caches.length; i++) assert.deepEqual(fs.readFileSync(caches[i]), before[i]);
        assert.equal(sessionIsProtected(root), true);
      }
    } finally {
      if (child && child.exitCode === null) child.kill();
      if (completed) await completed.catch(() => {});
      if (server) {
        server.closeAllConnections();
        await new Promise((resolve) => server.close(resolve));
      }
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });
}
