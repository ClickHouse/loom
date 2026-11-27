// The SessionStart hook saves the token it was given for the MCP header
// helper, and the primer says when the helper will send another one or a
// config token saved for another server than Claude Code's MCP entry calls:
// only where Claude Code runs the hook as its plugin's (CLAUDE_PLUGIN_ROOT),
// and never for an agent, whose tools send its own token.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { assertMode, hostEnv } from "./host-env.mjs";

const HOOK = fileURLToPath(new URL("../hooks/loom-session-start.mjs", import.meta.url));
const CODEX_HOOK = fileURLToPath(new URL("../../codex-plugin/hooks/loom-session-start.mjs", import.meta.url));
/** What marks a hook Claude Code runs for its plugin. */
const PLUGIN = { CLAUDE_PLUGIN_ROOT: fileURLToPath(new URL("..", import.meta.url)) };
/** Codex's hooks.json command: its own copy of the hook, named as the client. */
const CODEX = [CODEX_HOOK, "SessionStart", "codex"];
/** The hooks.generic.json command of a hand-wired client: no client, no CLAUDE_PLUGIN_ROOT. */
const GENERIC = [HOOK, "SessionStart"];
const SAVED_FOR = "https://a.example";

/** A Loom stand-in; `sent` holds the Authorization header of each request. */
async function server() {
  const sent = [];
  const srv = http.createServer((req, res) => {
    sent.push(req.headers.authorization);
    req.resume();
    req.on("end", () => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(
        req.url.startsWith("/oauth/token") ? JSON.stringify({ access_token: "agent.tok", expires_in: 3600 }) : "{}",
      );
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  return { srv, url: `http://127.0.0.1:${srv.address().port}`, sent };
}

/** A home holding `config`, or no ~/.loom at all without one. */
function home(config) {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-notice-")));
  if (!config) return dir;
  fs.mkdirSync(path.join(dir, ".loom"), { recursive: true });
  fs.writeFileSync(path.join(dir, ".loom", "config.json"), JSON.stringify(config), { mode: 0o600 });
  return dir;
}

/** The hook as Claude Code runs its plugin's, or the `command` given. */
function run(dir, env, command = null) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, command || [HOOK, "SessionStart"], {
      cwd: dir,
      env: hostEnv({
        PATH: process.env.PATH,
        HOME: dir,
        TMPDIR: dir,
        LOOM_CODE_MAP: "false",
        ...(command ? {} : PLUGIN),
        ...env,
      }),
    });
    let stdout = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.on("close", () => resolve(stdout));
    child.stdin.end("{}");
  });
}

const NOTICE = "holds a token saved for";

test("the primer names the mismatch for Claude Code's plugin, and not for Codex, a hand-wired client or an agent", async () => {
  const { srv, url } = await server();
  const dirs = [];
  const session = (env, args) => {
    const dir = home({ token: "cfg.tok", url: SAVED_FOR });
    dirs.push(dir);
    return run(dir, { LOOM_BASE_URL: url, ...env }, args);
  };
  try {
    const claude = await session({});
    assert.ok(claude.includes(`${NOTICE} ${SAVED_FOR}, which the loom MCP tools send to ${url}.`), claude);
    assert.equal(claude.includes("cfg.tok"), false);
    // the one-file setup: nothing names the entry's server, so the fix is to name it, not to swap the token
    const oneFile = home({ token: "cfg.tok", url: url });
    dirs.push(oneFile);
    const fallback = await run(oneFile, {});
    assert.ok(
      fallback.includes(
        `${NOTICE} ${url}, which the loom MCP tools send to http://localhost:7777, the MCP entry's default`,
      ),
      fallback,
    );
    assert.ok(fallback.includes(`--config server_url=${url}`), fallback);
    const codex = await session({}, CODEX);
    assert.ok(codex.length > 0, "the Codex primer still renders");
    assert.equal(codex.includes(NOTICE), false);
    const generic = await session({}, GENERIC);
    assert.ok(generic.length > 0, "the hand-wired client's primer still renders");
    assert.equal(generic.includes(NOTICE), false);
    const agent = await session({ LOOM_CLIENT_ID: "ci", LOOM_WORKLOAD_TOKEN: "proof" });
    assert.ok(agent.length > 0, "the agent primer still renders");
    assert.equal(agent.includes("could not be exchanged"), false, "the exchange succeeded");
    assert.equal(agent.includes(NOTICE), false);
  } finally {
    srv.close();
    for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("an install token written over a config that named another server is saved beside its own", async () => {
  const { srv, url } = await server();
  const dir = home({ url: SAVED_FOR });
  const env = { CLAUDE_PLUGIN_OPTION_SERVER_URL: url, CLAUDE_PLUGIN_OPTION_TOKEN: "opt.tok" };
  try {
    const first = await run(dir, env);
    assert.equal(first.includes(NOTICE), false, "the session that writes the token says nothing");
    const written = JSON.parse(fs.readFileSync(path.join(dir, ".loom", "config.json"), "utf8"));
    assert.deepEqual(written, { url, token: "opt.tok" });
    // the hook saved the install URL for the status script, in this session's tmp
    const stateFile = () => fs.readdirSync(dir).find((f) => /^loom-install-[0-9a-f]{12}\.json$/.test(f));
    const state = () => JSON.parse(fs.readFileSync(path.join(dir, stateFile()), "utf8"));
    assert.deepEqual(state(), { url, plugin: true });
    const second = await run(dir, env);
    assert.equal(second.includes(NOTICE), false, "the saved token is the one the MCP entry sends");
    // a session without the option drops the saved URL, and a hand-wired one the file
    await run(dir, { LOOM_BASE_URL: url });
    assert.deepEqual(state(), { url: "", plugin: true });
    await run(dir, { LOOM_BASE_URL: url }, GENERIC);
    assert.equal(stateFile(), undefined);
  } finally {
    srv.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("LOOM_API_KEY is saved for the helper of Claude Code's plugin, never over a saved token and never printed", async () => {
  const { srv, url, sent } = await server();
  const dirs = [];
  const session = (config, env, command) => {
    const dir = home(config);
    dirs.push(dir);
    return run(dir, { LOOM_BASE_URL: url, LOOM_API_KEY: "env.tok", ...env }, command).then((out) => ({ dir, out }));
  };
  const config = (dir) => path.join(dir, ".loom", "config.json");
  const SAYS = "the hooks use LOOM_API_KEY";
  try {
    // Claude Code keeps the variable from the helper: the hook saves it where the helper reads
    const first = await session(null, {});
    assert.deepEqual(JSON.parse(fs.readFileSync(config(first.dir), "utf8")), { token: "env.tok", url });
    assertMode(config(first.dir), 0o600);
    assertMode(path.dirname(config(first.dir)), 0o700);
    assert.ok(first.out.length > 0, "the primer renders");
    assert.equal(first.out.includes(SAYS), false, "the session that saves the token says nothing");
    assert.equal(first.out.includes("env.tok"), false);
    // a token saved already stays, and the primer names the fix without either token
    const other = await session({ token: "cfg.tok", url }, {});
    assert.equal(JSON.parse(fs.readFileSync(config(other.dir), "utf8")).token, "cfg.tok");
    assert.ok(other.out.includes(`${SAYS}, which Claude Code does not pass to the loom MCP tools.`), other.out);
    assert.ok(other.out.includes("and send another token saved there."), other.out);
    assert.ok(other.out.includes(`as \`token\` in ${config(other.dir)} and restart`), other.out);
    assert.equal(/env\.tok|cfg\.tok/.test(other.out), false);
    // the same token saved: nothing to say
    const same = await session({ token: "env.tok", url }, {});
    assert.equal(same.out.includes(SAYS), false);
    // the file LOOM_API_KEY_FILE names is kept from the helper too
    const named = path.join(home(null), "secret");
    dirs.push(path.dirname(named));
    fs.writeFileSync(named, "key.tok\n");
    const keyFile = await session(null, { LOOM_API_KEY: "", LOOM_API_KEY_FILE: named });
    assert.deepEqual(JSON.parse(fs.readFileSync(config(keyFile.dir), "utf8")), { token: "key.tok", url });
    assert.equal(keyFile.out.includes("key.tok"), false);
    // the hooks keep reading the named file: once it changes they send its token, and the primer says the tools send another
    fs.writeFileSync(named, "new.tok\n");
    sent.length = 0;
    const rotated = await run(keyFile.dir, { LOOM_BASE_URL: url, LOOM_API_KEY_FILE: named });
    assert.ok(sent.includes("Bearer new.tok"), String(sent));
    assert.equal(sent.includes("Bearer key.tok"), false);
    assert.equal(JSON.parse(fs.readFileSync(config(keyFile.dir), "utf8")).token, "key.tok");
    assert.ok(rotated.includes("the hooks use LOOM_API_KEY_FILE, which Claude Code does not pass"), rotated);
    assert.ok(rotated.includes("and send another token saved there."), rotated);
    assert.equal(/key\.tok|new\.tok/.test(rotated), false);
    // Codex, a hand-wired client and an agent keep the variable to themselves
    const codex = await session(null, {}, CODEX);
    assert.ok(codex.out.length > 0, "the Codex primer still renders");
    assert.equal(fs.existsSync(config(codex.dir)), false);
    const generic = await session(null, {}, GENERIC);
    assert.ok(generic.out.length > 0, "the hand-wired client's primer still renders");
    assert.equal(fs.existsSync(config(generic.dir)), false);
    const agent = await session(null, { LOOM_CLIENT_ID: "ci", LOOM_WORKLOAD_TOKEN: "proof" });
    assert.ok(agent.out.length > 0, "the agent primer still renders");
    assert.equal(fs.existsSync(config(agent.dir)), false);
    // a token saved beside theirs is theirs to send: nothing is said
    const beside = await session({ token: "cfg.tok", url }, {}, GENERIC);
    assert.equal(JSON.parse(fs.readFileSync(config(beside.dir), "utf8")).token, "cfg.tok");
    assert.equal((codex.out + generic.out + agent.out + beside.out).includes(SAYS), false);
  } finally {
    srv.close();
    for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
  }
});
