import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const here = path.dirname(fileURLToPath(import.meta.url));
const whichSync = (c) => spawnSync("/bin/sh", ["-c", `command -v ${c}`], { encoding: "utf8" }).stdout.trim();
const helper = path.join(here, "..", "skills", "loom-status", "scripts", "ensure-node.sh");

function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-node-bootstrap-"));
  const bin = path.join(dir, "bin");
  fs.mkdirSync(bin);
  const put = (name, body) => {
    const p = path.join(bin, name);
    fs.writeFileSync(p, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  };
  // Only what the test builds: no fixed directories.
  const env = { HOME: dir, PATH: bin, LOOM_NODE_SYSTEM_DIRS: "", TEST_BIN: bin, TMPDIR: dir };
  const runAt = (script, ...args) => spawnSync("/bin/sh", [script, ...args], { encoding: "utf8", env });
  const run = (...args) => runAt(helper, ...args);
  // The helper and the launcher it calls run under these.
  for (const command of ["sh", "dirname", "env"]) fs.symlinkSync(whichSync(command), path.join(bin, command));
  return { dir, bin, env, put, run, runAt };
}

test("loom-status Node helper is a no-op when the launcher finds Node 18+", () => {
  const f = fixture();
  try {
    f.put("node", "echo v22.0.0");
    const r = f.run();
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /Loom hook runtime ready: .*node \(v22\.0\.0\)/);
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});

test("loom-status Node check reports a missing runtime without installing", () => {
  const f = fixture();
  try {
    const r = f.run("--check");
    assert.equal(r.status, 1);
    assert.equal(r.stdout, "");
    assert.match(r.stderr, /^loom: Node\.js 18\+ not found .* set LOOM_NODE=\/path\/to\/node\.\n$/);
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});

test("loom-status Node helper selects apt and verifies the installed binary", () => {
  const f = fixture();
  try {
    f.put("id", "echo 0");
    f.put("uname", "echo Linux");
    f.put("curl", 'while [ "$1" != "-o" ]; do shift; done; shift; : > "$1"');
    f.put("bash", "exit 0");
    f.put(
      "apt-get",
      'if [ "$*" = "install -y nodejs" ]; then printf "#!/bin/sh\\necho v22.0.0\\n" > "$TEST_BIN/node"; chmod +x "$TEST_BIN/node"; fi',
    );
    for (const command of ["mktemp", "rm", "chmod"]) {
      fs.symlinkSync(`/usr/bin/${command}`, path.join(f.bin, command));
    }
    const r = f.run();
    assert.equal(r.status, 0, `${r.stderr}\n${r.stdout}`);
    assert.match(r.stdout, /Loom hook runtime ready: .*node \(v22\.0\.0\)\nRestart the coding-agent session/);
    assert.equal(fs.existsSync(path.join(f.bin, "node")), true);
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});

test("loom-status Node check names a too-old Node and the LOOM_NODE override", () => {
  const f = fixture();
  try {
    f.put("node", "echo v16.20.2");
    const r = f.run("--check");
    assert.equal(r.status, 1);
    assert.match(r.stderr, /the first Node found, .*node \(v16\.20\.2\), is older\. .*LOOM_NODE=/);
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});

test("loom-status Node check finds a version-manager Node the agent's PATH lacks", () => {
  const f = fixture();
  try {
    const bin = path.join(f.dir, ".local", "share", "fnm", "aliases", "default", "bin");
    fs.mkdirSync(bin, { recursive: true });
    fs.writeFileSync(path.join(bin, "node"), "#!/bin/sh\necho v24.1.0\n", { mode: 0o755 });
    const r = f.run("--check");
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout, `Loom hook runtime ready: ${path.join(bin, "node")} (v24.1.0)\n`);
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});

test("loom-status Node helper installs nothing while a broken LOOM_NODE would still win", () => {
  const f = fixture();
  try {
    f.put("id", "echo 0");
    f.put("uname", "echo Linux");
    f.put("apt-get", "echo apt-get ran >&2; exit 1");
    f.env.LOOM_NODE = path.join(f.dir, "gone");
    const r = f.run();
    assert.equal(r.status, 2);
    assert.match(
      r.stderr,
      /^LOOM_NODE=.*gone is not Node\.js 18\+, and it wins over any install; fix or unset it\.\n$/,
    );
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});

test("loom-status Node check warns when only this shell's environment finds Node", () => {
  const f = fixture();
  try {
    f.put("uname", "echo Darwin");
    // A Node on this shell's PATH only; it also reads settings.json for the check.
    f.put("node", `if [ "$1" = --version ]; then echo v22.0.0; else exec "${process.execPath}" "$@"; fi`);
    const settings = (env) => {
      fs.mkdirSync(path.join(f.dir, ".claude"), { recursive: true });
      fs.writeFileSync(path.join(f.dir, ".claude", "settings.json"), JSON.stringify({ env }));
    };
    // Codex: no GUI line, its hooks run through a login shell.
    settings({ LOOM_NODE_SYSTEM_DIRS: "" });
    assert.doesNotMatch(f.run("--check").stdout, /Found only/);
    f.env.CLAUDE_PLUGIN_ROOT = path.join(f.dir, "plugin");
    const shellOnly = f.run("--check");
    assert.equal(shellOnly.status, 0, shellOnly.stderr);
    assert.match(
      shellOnly.stdout,
      /ready: .*\nFound only with this shell's environment: Claude Code .*settings\.json\.\n$/,
    );
    // Claude Code's settings env reaches a GUI-started agent: LOOM_NODE or PATH there counts.
    settings({ LOOM_NODE_SYSTEM_DIRS: "", LOOM_NODE: path.join(f.bin, "node") });
    assert.doesNotMatch(f.run("--check").stdout, /Found only/);
    settings({ LOOM_NODE_SYSTEM_DIRS: "", PATH: f.bin });
    assert.doesNotMatch(f.run("--check").stdout, /Found only/);
    // A LOOM_NODE exported in this shell only does not.
    settings({ LOOM_NODE_SYSTEM_DIRS: "" });
    f.env.LOOM_NODE = path.join(f.bin, "node");
    assert.match(f.run("--check").stdout, /Found only/);
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});

// The helper and its launcher copied to where a client installs the plugin.
function installAt(root) {
  const scripts = path.join(root, "skills", "loom-status", "scripts");
  fs.mkdirSync(scripts, { recursive: true });
  fs.mkdirSync(path.join(root, "bin"));
  fs.copyFileSync(helper, path.join(scripts, "ensure-node.sh"));
  fs.copyFileSync(path.join(here, "..", "bin", "loom-node"), path.join(root, "bin", "loom-node"));
  return path.join(scripts, "ensure-node.sh");
}

test("loom-status Node check knows Claude Code by where it is installed", () => {
  const f = fixture();
  try {
    f.put("uname", "echo Linux");
    f.put("node", `if [ "$1" = --version ]; then echo v22.0.0; else exec "${process.execPath}" "$@"; fi`);
    const settings = (dir, env) => {
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, "settings.json"), JSON.stringify({ env }));
    };
    settings(path.join(f.dir, ".claude"), { LOOM_NODE_SYSTEM_DIRS: "" });
    const installed = installAt(path.join(f.dir, ".claude", "plugins", "cache", "loom", "loom", "1.2.7"));
    assert.match(f.runAt(installed, "--check").stdout, /Found only with this shell's environment: Claude Code/);
    // A custom config directory: its settings.json is the one Claude Code reads.
    f.env.CLAUDE_CONFIG_DIR = path.join(f.dir, "claude config");
    settings(f.env.CLAUDE_CONFIG_DIR, { LOOM_NODE_SYSTEM_DIRS: "" });
    const custom = installAt(path.join(f.env.CLAUDE_CONFIG_DIR, "plugins", "cache", "loom", "loom", "1.2.7"));
    const r = f.runAt(custom, "--check");
    assert.match(r.stdout, /Found only with this shell's environment: Claude Code/);
    assert.ok(r.stdout.includes(`env block of ${path.join(f.env.CLAUDE_CONFIG_DIR, "settings.json")}.`), r.stdout);
    settings(f.env.CLAUDE_CONFIG_DIR, { LOOM_NODE_SYSTEM_DIRS: "", LOOM_NODE: path.join(f.bin, "node") });
    assert.doesNotMatch(f.runAt(custom, "--check").stdout, /Found only/);
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});

test("loom-status Node check under Codex warns when the header helper's environment finds no Node", () => {
  const f = fixture();
  try {
    f.put("uname", "echo Linux");
    const elsewhere = path.join(f.dir, "elsewhere");
    fs.mkdirSync(elsewhere);
    fs.writeFileSync(path.join(elsewhere, "node"), "#!/bin/sh\necho v22.0.0\n", { mode: 0o755 });
    f.env.LOOM_NODE = path.join(elsewhere, "node");
    const r = f.run("--check");
    assert.equal(r.status, 0, r.stderr);
    assert.match(
      r.stdout,
      /ready: .*\nThe MCP header helper, which Codex runs without LOOM_NODE .*PATH Codex starts with\.\n$/,
    );
    f.env.PATH = `${f.bin}:${elsewhere}`;
    assert.doesNotMatch(f.run("--check").stdout, /header helper/);
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});
