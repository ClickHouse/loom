import "./temp-home.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { afterEach, test } from "node:test";

import {
  bindProtectionAlias,
  createProtectionRoot,
  protectionForAlias,
  protectSession,
  sessionIsProtected,
  SessionProtectionUnavailable,
} from "../lib/session-protection.mjs";

const owned = new Set();
function directory() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "loom-protection-test-"));
  owned.add(parent);
  // Windows creation must apply its real explicit DACL at creation. A Node
  // mkdir mode cannot establish that authority on a pre-existing directory.
  return process.platform === "win32" ? path.join(parent, "private") : parent;
}
function fixture() {
  const base = directory();
  const root = createProtectionRoot({ base });
  return { base, root, dir: path.join(base, root.id) };
}
afterEach(() => {
  for (const base of owned) fs.rmSync(base, { recursive: true, force: true });
  owned.clear();
});

if (process.platform === "win32") {
  test("native Windows refuses unchecked inherited ACLs without changing state", () => {
    const base = directory();
    fs.mkdirSync(base);
    const root = { base, id: "00000000-0000-0000-0000-000000000000" };
    const dir = path.join(base, root.id);
    fs.mkdirSync(dir);
    fs.writeFileSync(path.join(dir, "root.json"), JSON.stringify({ version: 1, id: root.id }));
    fs.writeFileSync(path.join(dir, "state"), "0\n");
    assert.throws(() => createProtectionRoot({ base }), SessionProtectionUnavailable);
    assert.equal(sessionIsProtected(root), true);
    assert.throws(() => protectSession(root), SessionProtectionUnavailable);
    assert.throws(() => bindProtectionAlias(root, "codex", "native-thread"), SessionProtectionUnavailable);
    assert.throws(() => protectionForAlias("codex", "native-thread", { base }), SessionProtectionUnavailable);
    assert.equal(fs.readFileSync(path.join(dir, "state"), "utf8"), "0\n");
    assert.deepEqual(fs.readdirSync(base), [root.id]);
  });
}
test("new state is private, content-free and clean", () => {
  const { root, dir } = fixture();
  assert.equal(sessionIsProtected(root), false);
  assert.deepEqual(fs.readdirSync(dir).sort(), ["root.json", "state"]);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(dir, "root.json"))), { version: 1, id: root.id });
  if (process.platform !== "win32") {
    for (const file of [dir, path.join(dir, "root.json"), path.join(dir, "state")]) {
      assert.equal(fs.statSync(file).mode & 0o077, 0);
    }
  }
});

test("protection persists across observers and never changes another lineage", () => {
  const { base, root, dir } = fixture();
  const other = createProtectionRoot({ base });
  const before = fs.readFileSync(path.join(dir, "state"));
  protectSession(root);
  protectSession(root);
  const restoredPointer = JSON.parse(JSON.stringify(root));
  assert.equal(sessionIsProtected(restoredPointer), true);
  assert.equal(sessionIsProtected(other), false);
  assert.ok(fs.readFileSync(path.join(dir, "state")).subarray(0, before.length).equals(before));
});

test("a child can durably protect the shared root before returning", () => {
  const { root } = fixture();
  const source = `import {protectSession} from ${JSON.stringify(new URL("../lib/session-protection.mjs", import.meta.url).href)};
      protectSession(JSON.parse(process.argv[1]));`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", source, JSON.stringify(root)], {
    encoding: "utf8",
    timeout: 5000,
  });
  assert.equal(child.error, undefined);
  assert.equal(child.status, 0, child.stderr);
  assert.equal(sessionIsProtected(root), true);
});

for (const value of ["", "0", "0\n1", "{}", "0\n1\n"]) {
  test(`an incomplete or non-clean state refuses (${JSON.stringify(value)})`, () => {
    const { root, dir } = fixture();
    fs.writeFileSync(path.join(dir, "state"), value);
    assert.equal(sessionIsProtected(root), true);
  });
}

for (const file of ["root.json", "state"]) {
  test(`a missing ${file} never becomes a new clean lineage`, () => {
    const { root, dir } = fixture();
    fs.unlinkSync(path.join(dir, file));
    assert.equal(sessionIsProtected(root), true);
    assert.throws(() => protectSession(root), SessionProtectionUnavailable);
  });
}

test("damaged or mismatched root identity refuses", () => {
  const { root, dir } = fixture();
  fs.writeFileSync(path.join(dir, "root.json"), "{");
  assert.equal(sessionIsProtected(root), true);
  assert.throws(() => protectSession(root), { message: "Session protection state could not be confirmed" });
  assert.equal(sessionIsProtected({ ...root, id: "../outside" }), true);
  assert.equal(sessionIsProtected(undefined), true);
});

for (const kind of process.platform === "win32" ? ["hardlink"] : ["symlink", "hardlink", "permissions"]) {
  test(`a planted ${kind} is refused without altering its target`, () => {
    const { root, dir } = fixture();
    const state = path.join(dir, "state");
    const target = path.join(dir, "target");
    fs.writeFileSync(target, "preserve", { mode: 0o600 });
    if (kind === "permissions") {
      fs.chmodSync(state, 0o666);
    } else {
      fs.unlinkSync(state);
      if (kind === "symlink") fs.symlinkSync(target, state);
      else fs.linkSync(target, state);
    }
    assert.equal(sessionIsProtected(root), true);
    assert.throws(() => protectSession(root), SessionProtectionUnavailable);
    assert.equal(fs.readFileSync(target, "utf8"), "preserve");
  });
}

test("unsafe roots and relative storage refuse without changing permissions", () => {
  if (process.platform !== "win32") {
    const base = directory();
    fs.chmodSync(base, 0o755);
    assert.throws(() => createProtectionRoot({ base }), SessionProtectionUnavailable);
    assert.equal(fs.statSync(base).mode & 0o777, 0o755);
  }
  assert.throws(() => createProtectionRoot({ base: "relative-state" }), SessionProtectionUnavailable);
});

test("old state has no time-based reset", () => {
  const { root, dir } = fixture();
  protectSession(root);
  for (const file of [dir, path.join(dir, "root.json"), path.join(dir, "state")]) fs.utimesSync(file, 0, 0);
  assert.equal(sessionIsProtected(root), true);
});

test("resume and descendants share their bound lineage across host identities", () => {
  const { base, root } = fixture();
  bindProtectionAlias(root, "codex", "parent-thread");
  bindProtectionAlias(root, "codex", "child-thread");
  bindProtectionAlias(root, "claude-code", "nested-session");
  const child = protectionForAlias("codex", "child-thread", { base });
  assert.equal(sessionIsProtected(child), false);
  protectSession(child);
  for (const [host, identity] of [
    ["codex", "parent-thread"],
    ["codex", "child-thread"],
    ["claude-code", "nested-session"],
  ]) {
    assert.equal(sessionIsProtected(protectionForAlias(host, identity, { base })), true);
  }
  const unrelated = createProtectionRoot({ base });
  bindProtectionAlias(unrelated, "claude-code", "parent-thread");
  assert.equal(sessionIsProtected(protectionForAlias("claude-code", "parent-thread", { base })), false);
});

test("an existing alias cannot replace a marked root with a clean one", () => {
  const { base, root } = fixture();
  bindProtectionAlias(root, "codex", "session");
  protectSession(root);
  bindProtectionAlias(root, "codex", "session");
  const clean = createProtectionRoot({ base });
  assert.throws(() => bindProtectionAlias(clean, "codex", "session"), SessionProtectionUnavailable);
  const resumed = protectionForAlias("codex", "session", { base });
  assert.equal(resumed.id, root.id);
  assert.equal(sessionIsProtected(resumed), true);
});

test("missing or damaged resume metadata never creates clean state", () => {
  const { base, root } = fixture();
  assert.throws(() => protectionForAlias("codex", "missing", { base }), SessionProtectionUnavailable);
  bindProtectionAlias(root, "codex", "session");
  const aliases = path.join(base, "aliases");
  const names = fs.readdirSync(aliases).filter((name) => name.endsWith(".json"));
  assert.equal(names.length, 1);
  const [name] = names;
  const file = path.join(aliases, name);
  const saved = fs.readFileSync(file);
  fs.writeFileSync(file, "{");
  assert.throws(() => protectionForAlias("codex", "session", { base }), SessionProtectionUnavailable);
  assert.throws(() => bindProtectionAlias(root, "codex", "session"), SessionProtectionUnavailable);
  assert.equal(fs.readFileSync(file, "utf8"), "{");
  fs.writeFileSync(file, saved);
  fs.unlinkSync(path.join(base, root.id, "state"));
  assert.equal(sessionIsProtected(protectionForAlias("codex", "session", { base })), true);
});
