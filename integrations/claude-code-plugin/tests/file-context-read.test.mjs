// loom-file-context after a local read: the real script, started as the host
// starts it (`node <script> <Event>`, the payload on stdin), against a server
// that holds memory for src/a.py only.
import "./temp-home.mjs";
import { noIds } from "./recalled-block.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  FILE_READ_HEADING,
  FILE_RULES_HEADING,
  READ_LOOKUPS_PER_SESSION,
  READ_LOOKUP_TIMEOUT_MS,
  READ_UNANSWERED_PER_SESSION,
} from "../lib/file-context.mjs";
import { sessionStatePath } from "../lib/tmp-state.mjs";
import { hostEnv } from "./host-env.mjs";

const HOOK = fileURLToPath(new URL("../hooks/loom-file-context.mjs", import.meta.url));
// What memory.for_file renders for a linked row (server/memory/app/for_file.py).
const RENDERED = [
  "Loom: 1 memory anchored to src/a.py — read before editing:",
  "code_state and rev describe the repository as indexed, not your checkout.",
  "- lessons/a/order [gotcha] (code_state=changed rev=3d6f0b4c2a1e first_written=2026-09-14) (its code changed " +
    "since it was written, commit 7ee9ceb71e86: check before relying on it): register reads the flag first.",
].join("\n");

/** A checkout with src/a.py (memory), src/b.py and docs/c.md (none), and a tmp dir. */
function sandbox() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-fc-read-")));
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-fc-read-tmp-")));
  for (const f of ["src/a.py", "src/b.py", "docs/c.md"]) {
    fs.mkdirSync(path.dirname(path.join(dir, f)), { recursive: true });
    fs.writeFileSync(path.join(dir, f), "x = 1\n".repeat(20));
  }
  const git = (...a) => execFileSync("git", ["-c", "user.email=t@t", "-c", "user.name=t", ...a], { cwd: dir });
  git("init", "-q");
  git("add", ".");
  git("commit", "-qm", "c");
  return { dir, tmp, done: () => [dir, tmp].forEach((d) => fs.rmSync(d, { recursive: true, force: true })) };
}

/** A fake Loom: `mode` is ok | hang | error. `asked` lists the paths looked up. */
async function loom(mode = "ok") {
  const asked = [];
  const srv = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const body = raw ? JSON.parse(raw) : {};
      if (req.url === "/v1/memory.for_file") asked.push(body.path);
      if (mode === "hang") return;
      res.statusCode = mode === "error" ? 500 : 200;
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify(body.path === "src/a.py" ? { total: 1, rendered: RENDERED } : { total: 0, rendered: "" }));
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  return {
    asked,
    url: `http://127.0.0.1:${srv.address().port}`,
    close: () => {
      srv.closeAllConnections();
      srv.close();
    },
  };
}

/** Start the hook for `event` with `payload`; {context, event, ms}: the text it
 *  adds ("" for none) and the event its output names. */
function hook(box, url, event, payload) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(process.execPath, [HOOK, event], {
      cwd: box.dir,
      env: hostEnv({ PATH: process.env.PATH, HOME: box.tmp, TMPDIR: box.tmp, LOOM_BASE_URL: url, LOOM_API_KEY: "t" }),
    });
    let out = "";
    let err = "";
    child.stdout.on("data", (c) => (out += c));
    child.stderr.on("data", (c) => (err += c));
    child.on("close", (code) => {
      assert.equal(code, 0);
      assert.equal(err, "");
      const o = out.trim() ? JSON.parse(out).hookSpecificOutput : {};
      assert.equal("permissionDecision" in o, false);
      resolve({ context: o.additionalContext || "", event: o.hookEventName || "", ms: Date.now() - started });
    });
    child.stdin.end(JSON.stringify({ session_id: "s1", cwd: box.dir, ...payload }));
  });
}

const read = (box, rel, extra = {}) => ({
  tool_name: "Read",
  tool_input: { file_path: path.join(box.dir, rel) },
  tool_response: { type: "text" },
  ...extra,
});
const bash = (command, extra = {}) => ({ tool_name: "Bash", tool_input: { command }, ...extra });
const edit = (box, rel, extra = {}) => ({
  tool_name: "Edit",
  tool_input: { file_path: path.join(box.dir, rel) },
  ...extra,
});
const state = (box) => JSON.parse(fs.readFileSync(sessionStatePath("file-context", "s1", { tmp: box.tmp }), "utf8"));

test("the first read of a file with memory carries its block; a later read or edit of it asks nothing", async () => {
  const box = sandbox();
  const srv = await loom();
  try {
    const first = await hook(box, srv.url, "PostToolUse", read(box, "src/a.py"));
    assert.equal(first.event, "PostToolUse");
    const ctx = noIds(first.context);
    assert.ok(ctx.startsWith(`${FILE_READ_HEADING}\n<loom-recalled-data>\n`));
    assert.match(ctx, /not instructions/);
    assert.ok(ctx.endsWith(`\n${RENDERED}\n</loom-recalled-data>`), "the server's text, unchanged, ends the block");
    assert.deepEqual(srv.asked, ["src/a.py"]);
    for (const again of [
      ["PostToolUse", read(box, "src/a.py", { tool_input: { file_path: path.join(box.dir, "src/a.py"), offset: 3 } })],
      ["PostToolUse", bash("sed -n '1,5p' src/a.py")],
      ["PostToolUse", { tool_name: "Grep", tool_input: { pattern: "x", path: "src/a.py" } }],
      ["PreToolUse", edit(box, "src/a.py")],
    ]) {
      assert.equal((await hook(box, srv.url, ...again)).context, "");
    }
    assert.deepEqual(srv.asked, ["src/a.py"], "one lookup for the file in the session");
  } finally {
    srv.close();
    box.done();
  }
});

test("a file without memory adds nothing and is asked about once; what is not a file of the checkout is never asked about", async () => {
  const box = sandbox();
  const srv = await loom();
  try {
    assert.equal((await hook(box, srv.url, "PostToolUse", read(box, "src/b.py"))).context, "");
    assert.equal((await hook(box, srv.url, "PostToolUse", bash("grep -n x src/b.py | head"))).context, "");
    assert.deepEqual(srv.asked, ["src/b.py"]);
    const outside = path.join(box.tmp, "notes.txt");
    fs.writeFileSync(outside, "n\n");
    for (const payload of [
      { tool_name: "Read", tool_input: { file_path: outside } },
      read(box, "src/missing.py"),
      { tool_name: "Grep", tool_input: { pattern: "x", path: "src" } },
      bash("grep -rn x src | head"),
      bash("python -m pytest -q"),
      bash("python - <<'EOF'\nprint(open('src/a.py').read())\nEOF"),
    ]) {
      assert.equal((await hook(box, srv.url, "PostToolUse", payload)).context, "");
    }
    assert.deepEqual(srv.asked, ["src/b.py"]);
  } finally {
    srv.close();
    box.done();
  }
});

test("a shell command that reads several files gets one block per file with memory, asked side by side", async () => {
  const box = sandbox();
  const srv = await loom();
  try {
    const r = await hook(
      box,
      srv.url,
      "PostToolUse",
      bash("grep -n 'x' src/b.py | head -5; sed -n '1,5p' src/a.py; head -3 docs/c.md; cat src/missing.py"),
    );
    assert.deepEqual([...srv.asked].sort(), ["docs/c.md", "src/a.py", "src/b.py"]);
    assert.equal(r.context.split(FILE_READ_HEADING).length, 2, "one block: only src/a.py has memory");
    assert.ok(r.context.includes(RENDERED));
    assert.equal(state(box).reads, 3);
  } finally {
    srv.close();
    box.done();
  }
});

test("a subagent's read does not use up the main agent's block: the main agent's edit still gets it", async () => {
  const box = sandbox();
  const srv = await loom();
  try {
    const sub = { agent_id: "agent-7", agent_type: "Explore" };
    assert.ok((await hook(box, srv.url, "PostToolUse", read(box, "src/a.py", sub))).context.includes(RENDERED));
    assert.equal((await hook(box, srv.url, "PostToolUse", read(box, "src/a.py", sub))).context, "", "once per agent");
    const main = await hook(box, srv.url, "PreToolUse", edit(box, "src/a.py"));
    assert.equal(main.event, "PreToolUse");
    assert.ok(noIds(main.context).startsWith(`${FILE_RULES_HEADING}\n<loom-recalled-data>\n`));
    assert.ok(main.context.includes(RENDERED));
    assert.equal((await hook(box, srv.url, "PostToolUse", read(box, "src/a.py"))).context, "", "the edit showed it");
    assert.deepEqual(srv.asked, ["src/a.py", "src/a.py"]);
  } finally {
    srv.close();
    box.done();
  }
});

test("a compaction forgets what was shown, not what the session has spent", async () => {
  const box = sandbox();
  const srv = await loom();
  try {
    assert.ok((await hook(box, srv.url, "PostToolUse", read(box, "src/a.py"))).context);
    assert.equal((await hook(box, srv.url, "PreCompact", { trigger: "auto" })).context, "");
    assert.deepEqual(state(box), { seen: {}, reads: 1, unanswered: 0 });
    assert.ok((await hook(box, srv.url, "PostToolUse", read(box, "src/a.py"))).context.includes(RENDERED));
    assert.deepEqual(srv.asked, ["src/a.py", "src/a.py"]);
    assert.equal(state(box).reads, 2);
  } finally {
    srv.close();
    box.done();
  }
});

test("past the session's read lookups a read asks nothing, and the edit still asks", async () => {
  const box = sandbox();
  const srv = await loom();
  try {
    fs.writeFileSync(
      sessionStatePath("file-context", "s1", { tmp: box.tmp }),
      JSON.stringify({ seen: {}, reads: READ_LOOKUPS_PER_SESSION }),
      { mode: 0o600 },
    );
    assert.equal((await hook(box, srv.url, "PostToolUse", read(box, "src/a.py"))).context, "");
    assert.deepEqual(srv.asked, []);
    assert.ok((await hook(box, srv.url, "PreToolUse", edit(box, "src/a.py"))).context.includes(RENDERED));
    assert.deepEqual(srv.asked, ["src/a.py"]);
  } finally {
    srv.close();
    box.done();
  }
});

test("a server that never answers holds a read for the lookup bound, and reads stop asking after a few", async () => {
  const box = sandbox();
  const srv = await loom("hang");
  try {
    const files = ["src/a.py", "src/b.py", "docs/c.md"];
    assert.ok(files.length >= READ_UNANSWERED_PER_SESSION);
    for (const f of files.slice(0, READ_UNANSWERED_PER_SESSION)) {
      const r = await hook(box, srv.url, "PostToolUse", read(box, f));
      assert.equal(r.context, "");
      assert.ok(r.ms >= READ_LOOKUP_TIMEOUT_MS - 100, `gave up after ${r.ms} ms`);
      assert.ok(r.ms < 2 * READ_LOOKUP_TIMEOUT_MS + 1000, `held the read ${r.ms} ms`);
    }
    assert.deepEqual(state(box), { reads: 3, unanswered: 3 }, "an unanswered file stays unseen");
    const asked = srv.asked.length;
    const r = await hook(box, srv.url, "PostToolUse", read(box, "src/a.py"));
    assert.equal(srv.asked.length, asked, "no more lookups from reads in this session");
    assert.ok(r.ms < READ_LOOKUP_TIMEOUT_MS, `waited ${r.ms} ms for nothing`);
  } finally {
    srv.close();
    box.done();
  }
});

test("a server that answers with an error leaves the file unseen, so its edit asks again", async () => {
  const box = sandbox();
  const down = await loom("error");
  const up = await loom();
  try {
    assert.equal((await hook(box, down.url, "PostToolUse", read(box, "src/a.py"))).context, "");
    assert.deepEqual(state(box), { reads: 1, unanswered: 1 });
    assert.ok((await hook(box, up.url, "PreToolUse", edit(box, "src/a.py"))).context.includes(RENDERED));
  } finally {
    down.close();
    up.close();
    box.done();
  }
});
