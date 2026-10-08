import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const here = path.dirname(fileURLToPath(import.meta.url));
const helper = path.join(here, "..", "skills", "loom-status", "scripts", "ensure-node.sh");

function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-node-bootstrap-"));
  const bin = path.join(dir, "bin");
  fs.mkdirSync(bin);
  const put = (name, body) => {
    const p = path.join(bin, name);
    fs.writeFileSync(p, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  };
  const run = (...args) =>
    spawnSync("/bin/sh", [helper, ...args], {
      encoding: "utf8",
      env: { ...process.env, PATH: bin, LOOM_NODE_HOOK_PATH: bin, TEST_BIN: bin, TMPDIR: dir },
    });
  return { dir, bin, put, run };
}

test("loom-status Node helper is a no-op when the hook shell has Node 18+", () => {
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
    assert.match(r.stderr, /Node\.js 18\+/);
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
    assert.match(r.stdout, /Restart the coding-agent session/);
    assert.equal(fs.existsSync(path.join(f.bin, "node")), true);
  } finally {
    fs.rmSync(f.dir, { recursive: true, force: true });
  }
});
