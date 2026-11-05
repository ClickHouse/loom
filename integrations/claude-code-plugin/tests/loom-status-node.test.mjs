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
  // Only what the test builds: no fixed directories, no n prefix of the host's.
  const env = { HOME: dir, PATH: bin, LOOM_NODE_SYSTEM_DIRS: "", N_PREFIX: dir, TEST_BIN: bin, TMPDIR: dir };
  const run = (...args) => spawnSync("/bin/sh", [helper, ...args], { encoding: "utf8", env });
  // The helper and the launcher it calls run under these.
  for (const command of ["sh", "dirname"]) fs.symlinkSync(whichSync(command), path.join(bin, command));
  return { dir, bin, env, put, run };
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
    assert.match(r.stderr, /the only Node found is .*node \(v16\.20\.2\)\. .*LOOM_NODE=/);
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
