import "./temp-home.mjs";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { afterEach, test } from "node:test";
import { hostEnv } from "./host-env.mjs";
import { bindHookProtection } from "../lib/hook-protection.mjs";
import {
  bindProtectionAlias,
  createProtectionRoot,
  establishProtectionAlias,
  findProtectionAlias,
  protectSession,
  sessionIsProtected,
  SessionProtectionUnavailable,
} from "../lib/session-protection.mjs";

const owned = new Set();
const running = new Set();
const identity = "native-startup";
const event = { session_id: identity, hook_event_name: "SessionStart", source: "startup" };
const moduleUrl = new URL("../lib/loom-client.mjs", import.meta.url).href;
const contextUrl = new URL("../lib/session-response.mjs", import.meta.url).href;

function home() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "loom-native-startup-"));
  owned.add(directory);
  return { directory, base: path.join(directory, ".loom", "session-protection", "v1") };
}

function keyFor(value = identity, host = "codex") {
  return crypto
    .createHash("sha256")
    .update(JSON.stringify([host, value]))
    .digest("hex");
}

function roots(base) {
  return fs.readdirSync(base).filter((name) => /^[0-9a-f-]{36}$/.test(name));
}

function entrySource(directory, name, before = "") {
  return `
    import fs from "node:fs";
    import { postBounded, runHookMain } from ${JSON.stringify(moduleUrl)};
    import { sessionProtectionContext } from ${JSON.stringify(contextUrl)};
    ${before}
    globalThis.fetch = async () => {
      fs.appendFileSync(${JSON.stringify(path.join(directory, name + ".sends"))}, "sent\\n");
      return { status: 200, json: async () => ({ ok: true }) };
    };
    process.stdout.write("READY\\n");
    await runHookMain(async () => {
      fs.writeFileSync(${JSON.stringify(path.join(directory, name + ".main"))},
        JSON.stringify(sessionProtectionContext().roots));
      await postBounded("context.pack", { mode: "startup" },
        { token: "fixture", serverUrl: "http://inert.invalid" });
    });
  `;
}

function entryOptions(directory) {
  return {
    env: hostEnv({ PATH: process.env.PATH, HOME: directory, USERPROFILE: directory }),
    encoding: "utf8",
    timeout: 15000,
  };
}

function entryArguments(source, host = "codex") {
  return ["--input-type=module", "-e", source, "startup-entry.mjs", "SessionStart", host];
}

function runEntry(directory, name, input = event, host = "codex") {
  const result = spawnSync(process.execPath, entryArguments(entrySource(directory, name), host), {
    ...entryOptions(directory),
    input: JSON.stringify(input),
  });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stderr, "");
}

function startEntry(directory, name, before = "", host = "codex") {
  const child = spawn(process.execPath, entryArguments(entrySource(directory, name, before), host), {
    ...entryOptions(directory),
    stdio: ["pipe", "pipe", "pipe"],
  });
  running.add(child);
  let stdout = "";
  let stderr = "";
  const watchers = new Map();
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
    for (const [marker, resolve] of watchers) {
      if (stdout.includes(marker)) {
        watchers.delete(marker);
        resolve();
      }
    }
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  const closed = new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code, signal) => {
      running.delete(child);
      resolve({ code, signal, stdout, stderr });
    });
  });
  const marker = async (text) => {
    if (stdout.includes(text)) return;
    let timer;
    try {
      await Promise.race([
        new Promise((resolve) => watchers.set(text, resolve)),
        closed.then((result) => {
          throw new Error(`entry closed before ${text}: ${JSON.stringify(result)}`);
        }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(`entry did not emit ${text}`)), 10000);
        }),
      ]);
    } finally {
      clearTimeout(timer);
      watchers.delete(text);
    }
  };
  return { child, closed, marker };
}

function readMain(directory, name) {
  const values = JSON.parse(fs.readFileSync(path.join(directory, name + ".main"), "utf8"));
  assert.equal(values.length, 1);
  assert.ok(fs.existsSync(path.join(directory, name + ".sends")));
  return values[0];
}

afterEach(async () => {
  await Promise.all(
    [...running].map(
      (child) =>
        new Promise((resolve) => {
          child.once("close", resolve);
          child.kill("SIGKILL");
        }),
    ),
  );
  for (const directory of owned) {
    fs.rmSync(directory, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
  owned.clear();
});

for (const host of ["codex", "claude-code"]) {
  test(`${host} startup establishes before its main and request without a factory`, () => {
    const { directory, base } = home();
    assert.equal(fs.existsSync(base), false);
    runEntry(directory, "startup", event, host);
    const root = readMain(directory, "startup");
    assert.deepEqual(findProtectionAlias(host, identity, { base }), root);
    assert.equal(sessionIsProtected(root), false);
    assert.deepEqual(roots(base), [root.id]);
    assert.deepEqual(fs.readdirSync(path.join(base, "aliases")).sort(), [
      keyFor(identity, host) + ".claim",
      keyFor(identity, host) + ".json",
    ]);
    const marker = fs.lstatSync(path.join(base, "aliases", keyFor(identity, host) + ".claim"));
    assert.equal(marker.isFile(), true);
    assert.equal(marker.size, 0);
    if (process.platform !== "win32") assert.equal(marker.mode & 0o077, 0);
  });

  test(`${host} concurrent startup entries establish the same immutable family`, async () => {
    const { directory, base } = home();
    const entries = [startEntry(directory, "session-start", "", host), startEntry(directory, "run", "", host)];
    await Promise.all(entries.map((entry) => entry.marker("READY\n")));
    for (const entry of entries) entry.child.stdin.end(JSON.stringify(event));
    for (const result of await Promise.all(entries.map((entry) => entry.closed))) {
      assert.equal(result.code, 0, result.stderr);
      assert.equal(result.stderr, "");
    }
    const first = readMain(directory, "session-start");
    assert.deepEqual(readMain(directory, "run"), first);
    assert.deepEqual(roots(base), [first.id]);
    assert.deepEqual(findProtectionAlias(host, identity, { base }), first);
  });

  test(`${host} startup never resets a protected alias or creates another root`, () => {
    const { directory, base } = home();
    runEntry(directory, "first", event, host);
    const root = readMain(directory, "first");
    protectSession(root);
    const state = fs.readFileSync(path.join(base, root.id, "state"));
    runEntry(directory, "again", event, host);
    assert.equal(fs.existsSync(path.join(directory, "again.main")), false);
    assert.equal(fs.existsSync(path.join(directory, "again.sends")), false);
    assert.deepEqual(findProtectionAlias(host, identity, { base }), root);
    assert.deepEqual(fs.readFileSync(path.join(base, root.id, "state")), state);
    assert.deepEqual(roots(base), [root.id]);
  });

  test(`${host} startup refuses a lost alias without replacing its protected family`, () => {
    const { directory, base } = home();
    runEntry(directory, "first", event, host);
    const root = readMain(directory, "first");
    assert.deepEqual(establishProtectionAlias(host, identity, { base }), root);
    protectSession(root);
    assert.equal(sessionIsProtected(root), true);
    const aliases = path.join(base, "aliases");
    const alias = path.join(aliases, keyFor(identity, host) + ".json");
    const claim = path.join(aliases, keyFor(identity, host) + ".claim");
    const receipt = fs.lstatSync(claim, { bigint: true });
    const claimBytes = fs.readFileSync(claim);
    const state = path.join(base, root.id, "state");
    const manifest = path.join(base, root.id, "root.json");
    const stateBytes = fs.readFileSync(state);
    const manifestBytes = fs.readFileSync(manifest);
    fs.unlinkSync(alias);
    runEntry(directory, "again", event, host);
    assert.equal(fs.existsSync(path.join(directory, "again.main")), false);
    assert.equal(fs.existsSync(path.join(directory, "again.sends")), false);
    assert.throws(() => establishProtectionAlias(host, identity, { base }), SessionProtectionUnavailable);
    assert.equal(fs.existsSync(alias), false);
    assert.deepEqual(fs.readdirSync(aliases), [path.basename(claim)]);
    assert.deepEqual(roots(base), [root.id]);
    assert.equal(sessionIsProtected(root), true);
    assert.deepEqual(fs.readFileSync(state), stateBytes);
    assert.deepEqual(fs.readFileSync(manifest), manifestBytes);
    assert.deepEqual(fs.readFileSync(claim), claimBytes);
    const retained = fs.lstatSync(claim, { bigint: true });
    assert.equal(retained.dev, receipt.dev);
    assert.equal(retained.ino, receipt.ino);
  });
}

for (const host of ["codex", "claude-code"]) {
  for (const [name, input, options] of [
    ...["resume", "fork", "clear", "compact"].map((source) => [source, { ...event, source }, {}]),
    ["missing payload event", { session_id: identity, source: "startup" }, {}],
    ["mismatched payload event", { ...event, hook_event_name: "Stop" }, {}],
    ["missing source", { session_id: identity, hook_event_name: "SessionStart" }, {}],
    ["subagent", { ...event, hook_event_name: "SubagentStart" }, { event: "SubagentStart" }],
    ["other hook", event, { event: "UserPromptSubmit" }],
    ["unsupported host", event, { host: "unsupported" }],
    ["task host", event, { host: "cline" }],
  ]) {
    test(`${host} unknown ${name} cannot bootstrap`, () => {
      const { base } = home();
      assert.throws(
        () => bindHookProtection(input, { host, event: "SessionStart", base, ...options }),
        SessionProtectionUnavailable,
      );
      assert.equal(fs.existsSync(base), false);
    });
  }

  test(`${host} startup cannot replace other contributing roots with a fresh family`, () => {
    const { base } = home();
    const known = createProtectionRoot({ base });
    assert.throws(
      () => bindHookProtection(event, { host, event: "SessionStart", base, knownRoots: [known] }),
      SessionProtectionUnavailable,
    );
    assert.equal(sessionIsProtected(known), true);
    assert.equal(findProtectionAlias(host, identity, { base }), undefined);
    assert.deepEqual(roots(base), [known.id]);
  });
}

for (const damage of ["alias", "manifest"]) {
  test(`a damaged ${damage} is not repaired by startup`, () => {
    const { base } = home();
    const root = establishProtectionAlias("codex", identity, { base });
    const file =
      damage === "alias" ? path.join(base, "aliases", keyFor() + ".json") : path.join(base, root.id, "root.json");
    fs.writeFileSync(file, "{");
    assert.throws(() => establishProtectionAlias("codex", identity, { base }), SessionProtectionUnavailable);
    assert.equal(fs.readFileSync(file, "utf8"), "{");
    assert.deepEqual(roots(base), [root.id]);
  });
}

test("an abandoned claim without a published alias is retained and never taken over", () => {
  const { base } = home();
  const prior = establishProtectionAlias("codex", "other", { base });
  const claim = path.join(base, "aliases", keyFor() + ".claim");
  if (process.platform === "win32") fs.writeFileSync(claim, "preserve");
  else fs.writeFileSync(claim, "", { mode: 0o600, flag: "wx" });
  const receipt = fs.lstatSync(claim);
  assert.throws(() => establishProtectionAlias("codex", identity, { base }), SessionProtectionUnavailable);
  assert.throws(() => bindProtectionAlias(prior, "codex", identity), SessionProtectionUnavailable);
  assert.equal(fs.lstatSync(claim).ino, receipt.ino);
  assert.deepEqual(roots(base), [prior.id]);
  assert.equal(findProtectionAlias("codex", identity, { base }), undefined);
});

test("a retained claim after complete publication reuses the alias without deleting the claim", () => {
  const { base } = home();
  const root = establishProtectionAlias("codex", identity, { base });
  const claim = path.join(base, "aliases", keyFor() + ".claim");
  if (process.platform === "win32") fs.writeFileSync(claim, "preserve");
  const receipt = fs.lstatSync(claim);
  protectSession(root);
  assert.deepEqual(establishProtectionAlias("codex", identity, { base }), root);
  bindProtectionAlias(root, "codex", identity);
  const other = createProtectionRoot({ base });
  assert.throws(() => bindProtectionAlias(other, "codex", identity), SessionProtectionUnavailable);
  assert.deepEqual(findProtectionAlias("codex", identity, { base }), root);
  assert.equal(sessionIsProtected(root), true);
  assert.equal(fs.lstatSync(claim).ino, receipt.ino);
});

if (process.platform !== "win32") {
  test("a claim removed after contention does not fail a valid startup", () => {
    const { base } = home();
    const aliases = path.join(base, "aliases");
    const claim = path.join(aliases, keyFor() + ".claim");
    fs.mkdirSync(aliases, { recursive: true, mode: 0o700 });
    fs.writeFileSync(claim, "", { mode: 0o600, flag: "wx" });
    const open = fs.openSync;
    let removed = false;
    fs.openSync = function (file, ...args) {
      if (file === claim && !removed) {
        removed = true;
        fs.unlinkSync(claim);
        const error = new Error("claim disappeared during contention");
        error.code = "EEXIST";
        throw error;
      }
      return open.call(this, file, ...args);
    };
    let root;
    try {
      root = bindHookProtection(event, { host: "codex", event: "SessionStart", base });
    } finally {
      fs.openSync = open;
    }
    assert.equal(removed, true);
    assert.deepEqual(findProtectionAlias("codex", identity, { base }), root);
    assert.deepEqual(roots(base), [root.id]);
  });

  test("atomic claim creation never adopts a replacement before receipt validation", () => {
    const { base } = home();
    const claim = path.join(base, "aliases", keyFor() + ".claim");
    const retained = claim + ".retained";
    const open = fs.openSync;
    let replacement;
    fs.openSync = function (file, flags, ...args) {
      const fd = open.call(this, file, flags, ...args);
      if (file === claim) {
        assert.ok(flags & fs.constants.O_CREAT);
        assert.ok(flags & fs.constants.O_EXCL);
        assert.ok(flags & fs.constants.O_NOFOLLOW);
        fs.renameSync(claim, retained);
        const other = open.call(this, claim, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
        fs.closeSync(other);
        replacement = fs.lstatSync(claim);
      }
      return fd;
    };
    try {
      assert.throws(
        () => bindHookProtection(event, { host: "codex", event: "SessionStart", base }),
        SessionProtectionUnavailable,
      );
    } finally {
      fs.openSync = open;
    }
    assert.ok(replacement);
    assert.equal(fs.lstatSync(claim).ino, replacement.ino);
    assert.equal(fs.lstatSync(retained).isFile(), true);
    assert.deepEqual(roots(base), []);
    assert.equal(findProtectionAlias("codex", identity, { base }), undefined);
  });

  test("replacement after the final receipt check is preserved by close-only cleanup", () => {
    const { base } = home();
    const claim = path.join(base, "aliases", keyFor() + ".claim");
    const retained = claim + ".retained";
    const lstat = fs.lstatSync;
    let checks = 0;
    let replacement;
    fs.lstatSync = function (file, ...args) {
      const st = lstat.call(this, file, ...args);
      if (file === claim && ++checks === 2) {
        fs.renameSync(claim, retained);
        fs.writeFileSync(claim, "", { mode: 0o600, flag: "wx" });
        replacement = lstat.call(this, claim);
      }
      return st;
    };
    let root;
    try {
      root = bindHookProtection(event, { host: "codex", event: "SessionStart", base });
    } finally {
      fs.lstatSync = lstat;
    }
    assert.equal(checks, 2);
    assert.ok(replacement);
    assert.equal(fs.lstatSync(claim).ino, replacement.ino);
    assert.equal(fs.lstatSync(retained).isFile(), true);
    assert.deepEqual(findProtectionAlias("codex", identity, { base }), root);
    assert.deepEqual(roots(base), [root.id]);
  });

  test("a competing entry waits through partial publication and keeps the same root", async () => {
    const { directory, base } = home();
    const alias = path.join(base, "aliases", keyFor() + ".json");
    const release = path.join(directory, "release");
    const first = startEntry(
      directory,
      "first",
      `
      const files = new Map();
      const open = fs.openSync;
      fs.openSync = function(file, ...args) {
        const fd = open.call(this, file, ...args); files.set(fd, String(file)); return fd;
      };
      const write = fs.writeFileSync;
      fs.writeFileSync = function(file, value, ...args) {
        if (files.get(file) === ${JSON.stringify(alias)}) {
          const split = Math.floor(value.length / 2);
          write.call(this, file, value.slice(0, split), ...args);
          process.stdout.write("PARTIAL\\n");
          const until = Date.now() + 5000;
          while (!fs.existsSync(${JSON.stringify(release)})) {
            if (Date.now() >= until) throw new Error("publication barrier expired");
            Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
          }
          return write.call(this, file, value.slice(split), ...args);
        }
        return write.call(this, file, value, ...args);
      };
    `,
    );
    await first.marker("READY\n");
    first.child.stdin.end(JSON.stringify(event));
    await first.marker("PARTIAL\n");
    const second = startEntry(
      directory,
      "second",
      `
      const read = fs.readSync;
      fs.readSync = function(file, bytes, ...args) {
        const count = read.call(this, file, bytes, ...args);
        const value = bytes.toString("utf8");
        if (value.startsWith('{"version":1,"identity":') && !value.endsWith("}")) {
          fs.writeFileSync(${JSON.stringify(release)}, "release");
          process.stdout.write("PARTIAL_OBSERVED\\n");
        }
        return count;
      };
    `,
    );
    await second.marker("READY\n");
    second.child.stdin.end(JSON.stringify(event));
    await second.marker("PARTIAL_OBSERVED\n");
    for (const result of await Promise.all([first.closed, second.closed])) {
      assert.equal(result.code, 0, result.stderr);
      assert.equal(result.stderr, "");
    }
    const root = readMain(directory, "first");
    assert.deepEqual(readMain(directory, "second"), root);
    assert.deepEqual(roots(base), [root.id]);
  });

  test("a replacement during publication is detected and never deleted", () => {
    const { base } = home();
    const alias = path.join(base, "aliases", keyFor() + ".json");
    const claim = path.join(base, "aliases", keyFor() + ".claim");
    const retained = claim + ".retained";
    const open = fs.openSync;
    const write = fs.writeFileSync;
    const files = new Map();
    let replacement;
    fs.openSync = function (file, ...args) {
      const fd = open.call(this, file, ...args);
      files.set(fd, String(file));
      return fd;
    };
    fs.writeFileSync = function (file, ...args) {
      const value = write.call(this, file, ...args);
      if (files.get(file) === alias) {
        fs.renameSync(claim, retained);
        fs.writeFileSync(claim, "", { mode: 0o600, flag: "wx" });
        replacement = fs.lstatSync(claim);
      }
      return value;
    };
    try {
      assert.throws(
        () => bindHookProtection(event, { host: "codex", event: "SessionStart", base }),
        SessionProtectionUnavailable,
      );
    } finally {
      fs.openSync = open;
      fs.writeFileSync = write;
    }
    assert.ok(replacement);
    assert.equal(fs.lstatSync(claim).ino, replacement.ino);
    assert.equal(fs.lstatSync(retained).isFile(), true);
  });

  test("a planted claim symlink is refused without changing its target", () => {
    const { directory, base } = home();
    establishProtectionAlias("codex", "other", { base });
    const target = path.join(directory, "keep");
    fs.mkdirSync(target, { mode: 0o700 });
    fs.writeFileSync(path.join(target, "evidence"), "preserve");
    const claim = path.join(base, "aliases", keyFor() + ".claim");
    fs.symlinkSync(target, claim);
    assert.throws(() => establishProtectionAlias("codex", identity, { base }), SessionProtectionUnavailable);
    assert.equal(fs.lstatSync(claim).isSymbolicLink(), true);
    assert.equal(fs.readFileSync(path.join(target, "evidence"), "utf8"), "preserve");
  });

  for (const point of ["claim", "published"]) {
    test(`a producer crash at ${point} preserves the correct startup authority`, async () => {
      const { directory, base } = home();
      const pause = `
        let claimFd;
        const open = fs.openSync;
        fs.openSync = function(file, ...args) {
          const fd = open.call(this, file, ...args);
          if (String(file).endsWith(".claim")) {
            claimFd = fd;
            if (${JSON.stringify(point)} === "claim") {
              process.stdout.write("PAUSED\\n");
              for (;;) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1000);
            }
          }
          return fd;
        };
        const close = fs.closeSync;
        fs.closeSync = function(fd) {
          if (fd === claimFd && ${JSON.stringify(point)} === "published") {
            process.stdout.write("PAUSED\\n");
            for (;;) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1000);
          }
          return close.call(this, fd);
        };
      `;
      const producer = startEntry(directory, "producer", pause);
      await producer.marker("READY\n");
      producer.child.stdin.end(JSON.stringify(event));
      await producer.marker("PAUSED\n");
      const claim = path.join(base, "aliases", keyFor() + ".claim");
      const receipt = fs.lstatSync(claim);
      const published = findProtectionAlias("codex", identity, { base });
      assert.equal(Boolean(published), point === "published");
      producer.child.kill("SIGKILL");
      const stopped = await producer.closed;
      assert.equal(stopped.signal, "SIGKILL");
      runEntry(directory, "next");
      if (point === "published") {
        assert.deepEqual(readMain(directory, "next"), published);
        assert.deepEqual(roots(base), [published.id]);
      } else {
        assert.equal(fs.existsSync(path.join(directory, "next.main")), false);
        assert.equal(fs.existsSync(path.join(directory, "next.sends")), false);
        assert.deepEqual(roots(base), []);
      }
      assert.equal(fs.lstatSync(claim).ino, receipt.ino);
    });
  }
}
