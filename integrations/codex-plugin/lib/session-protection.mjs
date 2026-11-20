// Durable, content-free state shared by one session lineage. Unlike hook caches,
// this state has no expiry, reset or best-effort write after private output.

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { readPrivateFile } from "./tmp-state.mjs";
import { windowsSessionProtection, windowsFailureDiagnostic } from "./windows-session-protection.mjs";

const ROOT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const VERSION = 1;
const ALIAS_CLAIM_MS = 500;
const defaultBase = () => path.join(os.homedir(), ".loom", "session-protection", "v1");

export class SessionProtectionUnavailable extends Error {
  constructor() {
    super("Session protection state could not be confirmed");
    this.name = "SessionProtectionUnavailable";
  }
}

function protectionUnavailable(error) {
  const unavailable = new SessionProtectionUnavailable();
  const diagnostic = windowsFailureDiagnostic(error);
  if (diagnostic) {
    Object.defineProperty(unavailable, "cause", { value: diagnostic, enumerable: true });
    // TAP preserves the original error code when it unwraps a test failure.
    const code = `LSP1:${diagnostic.phase}:${diagnostic.exitCode ?? "_"}:${diagnostic.workerExit ?? "_"}:${diagnostic.signal}:${diagnostic.errorCode ?? "_"}:${diagnostic.elapsedMs}:${Number(diagnostic.timedOut)}:${diagnostic.stdoutUnits}:${diagnostic.stderrUnits}:${diagnostic.validFrames}:${diagnostic.rejectedLines}:${diagnostic.helperErrorCode ?? "_"}:${diagnostic.lastValidatedPhase}`;
    Object.defineProperty(unavailable, "code", { value: code, enumerable: true });
  }
  return unavailable;
}

function privateDirectory(directory) {
  // POSIX mode bits do not establish Windows DACL authority.
  if (process.platform === "win32" || typeof process.getuid !== "function") throw new SessionProtectionUnavailable();
  const st = fs.lstatSync(directory);
  if (!st.isDirectory() || st.isSymbolicLink() || (st.mode & 0o077) !== 0) throw new SessionProtectionUnavailable();
  if (typeof process.getuid === "function" && st.uid !== process.getuid()) throw new SessionProtectionUnavailable();
  return st;
}

function syncDirectory(directory) {
  const fd = fs.openSync(directory, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
  try {
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

function privateText(file) {
  if ((fs.lstatSync(file).mode & 0o077) !== 0) throw new SessionProtectionUnavailable();
  return readPrivateFile(file);
}

function location(root) {
  if (!root || typeof root.base !== "string" || !path.isAbsolute(root.base) || !ROOT_ID.test(root.id)) {
    throw new SessionProtectionUnavailable();
  }
  privateDirectory(root.base);
  const directory = path.join(root.base, root.id);
  privateDirectory(directory);
  const manifest = JSON.parse(privateText(path.join(directory, "root.json")));
  if (
    !manifest ||
    manifest.version !== VERSION ||
    manifest.id !== root.id ||
    Object.keys(manifest).sort().join(",") !== "id,version"
  ) {
    throw new SessionProtectionUnavailable();
  }
  return directory;
}

function durableCreate(file, value) {
  // Exclusive creation never truncates an existing security record.
  const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
  try {
    fs.writeFileSync(fd, value);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  syncDirectory(path.dirname(file));
}

/** Create before any private result; the host binding owns lineage selection. */
export function createProtectionRoot({ base = defaultBase() } = {}) {
  try {
    if (!path.isAbsolute(base)) throw new SessionProtectionUnavailable();
    if (process.platform === "win32") {
      return Object.freeze({ base, id: windowsSessionProtection("create", { base }) });
    }
    fs.mkdirSync(base, { recursive: true, mode: 0o700 });
    privateDirectory(base);
    const root = Object.freeze({ base, id: crypto.randomUUID() });
    const directory = path.join(base, root.id);
    fs.mkdirSync(directory, { mode: 0o700 });
    durableCreate(path.join(directory, "state"), "0\n");
    durableCreate(path.join(directory, "root.json"), JSON.stringify({ version: VERSION, id: root.id }));
    syncDirectory(base);
    if (sessionIsProtected(root)) throw new SessionProtectionUnavailable();
    return root;
  } catch (error) {
    throw protectionUnavailable(error);
  }
}

/** Unknown, damaged or inaccessible state suppresses automatic sends. */
export function sessionIsProtected(root) {
  try {
    if (process.platform === "win32") return windowsSessionProtection("inspect", root);
    const directory = location(root);
    return privateText(path.join(directory, "state")) !== "0\n";
  } catch {
    return true;
  }
}

/** Complete this durable write before exposing a mirrored response to its caller. */
export function protectSession(root) {
  try {
    if (process.platform === "win32") {
      windowsSessionProtection("protect", root);
      return;
    }
    const directory = location(root);
    // A missing record is unknown, never a clean session. Append-only state
    // also turns a partial write into protection rather than a false absence.
    const fd = fs.openSync(
      path.join(directory, "state"),
      fs.constants.O_WRONLY | fs.constants.O_APPEND | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0),
    );
    try {
      const st = fs.fstatSync(fd);
      if (!st.isFile() || st.nlink !== 1 || (st.mode & 0o077) !== 0) throw new SessionProtectionUnavailable();
      if (typeof process.getuid === "function" && st.uid !== process.getuid()) throw new SessionProtectionUnavailable();
      if (fs.writeSync(fd, "1\n") !== 2) throw new SessionProtectionUnavailable();
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
  } catch (error) {
    throw protectionUnavailable(error);
  }
}

function aliasKey(host, identity) {
  if (
    typeof host !== "string" ||
    !/^[a-z][a-z0-9-]{0,63}$/.test(host) ||
    typeof identity !== "string" ||
    !identity ||
    identity.length > 1024
  ) {
    throw new SessionProtectionUnavailable();
  }
  return crypto
    .createHash("sha256")
    .update(JSON.stringify([host, identity]))
    .digest("hex");
}

function aliasDirectory(base, create = false) {
  if (typeof base !== "string" || !path.isAbsolute(base)) throw new SessionProtectionUnavailable();
  privateDirectory(base);
  const directory = path.join(base, "aliases");
  if (create) fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  privateDirectory(directory);
  return directory;
}

function parseAlias(text, key) {
  const record = JSON.parse(text);
  if (
    !record ||
    record.version !== VERSION ||
    record.identity !== key ||
    !ROOT_ID.test(record.root) ||
    Object.keys(record).sort().join(",") !== "identity,root,version"
  ) {
    throw new SessionProtectionUnavailable();
  }
  return record.root;
}

function readAlias(file, key) {
  return parseAlias(privateText(file), key);
}

function sameDirectory(directory, receipt) {
  const current = privateDirectory(directory);
  if (current.dev !== receipt.dev || current.ino !== receipt.ino) throw new SessionProtectionUnavailable();
}

function confirmPublishedAlias(base, key) {
  const directory = aliasDirectory(base);
  const file = path.join(directory, key + ".json");
  const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0));
  let root;
  try {
    const held = fs.fstatSync(fd);
    const current = fs.lstatSync(file);
    if (
      !held.isFile() ||
      held.nlink !== 1 ||
      held.size > 65536 ||
      (held.mode & 0o077) !== 0 ||
      held.uid !== process.getuid() ||
      held.dev !== current.dev ||
      held.ino !== current.ino
    )
      throw new SessionProtectionUnavailable();
    const bytes = Buffer.alloc(held.size);
    if (fs.readSync(fd, bytes, 0, bytes.length, 0) !== bytes.length || fs.fstatSync(fd).size !== bytes.length) {
      throw new SessionProtectionUnavailable();
    }
    root = Object.freeze({ base, id: parseAlias(bytes.toString("utf8"), key) });
    location(root);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  syncDirectory(directory);
  syncDirectory(base);
  return root;
}

function privateClaim(claim) {
  const st = fs.lstatSync(claim);
  if (!st.isFile() || st.nlink !== 1 || st.size !== 0 || (st.mode & 0o077) !== 0 || st.uid !== process.getuid()) {
    throw new SessionProtectionUnavailable();
  }
  return st;
}

function withAliasClaim(base, key, operation, boundRoot) {
  const directory = aliasDirectory(base, true);
  const flags = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | (fs.constants.O_NOFOLLOW ?? 0);
  const directoryFd = fs.openSync(directory, flags);
  let directoryReceipt;
  const claim = path.join(directory, key + ".claim");
  let claimFd;
  let receipt;
  const checkReceipt = () => {
    sameDirectory(directory, directoryReceipt);
    const current = privateClaim(claim);
    const held = fs.fstatSync(claimFd);
    if (
      current.dev !== receipt.dev ||
      current.ino !== receipt.ino ||
      held.dev !== receipt.dev ||
      held.ino !== receipt.ino
    ) {
      throw new SessionProtectionUnavailable();
    }
  };
  try {
    directoryReceipt = fs.fstatSync(directoryFd);
    sameDirectory(directory, directoryReceipt);
    const deadline = performance.now() + ALIAS_CLAIM_MS;
    const wait = new Int32Array(new SharedArrayBuffer(4));
    for (;;) {
      if (performance.now() >= deadline) throw new SessionProtectionUnavailable();
      sameDirectory(directory, directoryReceipt);
      try {
        // Exclusive open returns the exact created object, with no mkdir/open gap.
        claimFd = fs.openSync(
          claim,
          fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | (fs.constants.O_NOFOLLOW ?? 0),
          0o600,
        );
        break;
      } catch (error) {
        if (error?.code !== "EEXIST") throw error;
        try {
          privateClaim(claim);
        } catch (claimError) {
          if (claimError?.code !== "ENOENT") throw claimError;
        }
        let published;
        try {
          published = confirmPublishedAlias(base, key);
        } catch {
          // A busy publisher can expose incomplete bytes; never repair them.
        }
        if (published) {
          if (boundRoot && boundRoot.id !== published.id) throw new SessionProtectionUnavailable();
          return published;
        }
        const remaining = deadline - performance.now();
        if (remaining <= 0) throw new SessionProtectionUnavailable();
        Atomics.wait(wait, 0, 0, Math.min(10, remaining));
      }
    }
    receipt = fs.fstatSync(claimFd);
    checkReceipt();
    fs.fsyncSync(claimFd);
    syncDirectory(directory);
    const result = operation();
    checkReceipt();
    return result;
  } finally {
    try {
      // Retain the marker on success, failure and crash. A complete alias is
      // reusable; an unpublished claim never authorizes another initializer.
      if (claimFd !== undefined) fs.closeSync(claimFd);
    } finally {
      fs.closeSync(directoryFd);
    }
  }
}

function publishAlias(root, key) {
  location(root);
  const directory = aliasDirectory(root.base, true);
  const file = path.join(directory, key + ".json");
  try {
    durableCreate(file, JSON.stringify({ version: VERSION, identity: key, root: root.id }));
  } catch (error) {
    if (error?.code !== "EEXIST" || readAlias(file, key) !== root.id) throw error;
    // Confirm another initializer's data before a caller relies on the alias.
    const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
    try {
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    syncDirectory(directory);
  }
  syncDirectory(root.base);
}

/** Bind only a verified host identity; an existing alias can never choose a new root. */
export function bindProtectionAlias(root, host, identity) {
  try {
    if (process.platform === "win32") {
      windowsSessionProtection("bind", { ...root, key: aliasKey(host, identity) });
      return;
    }
    location(root);
    const key = aliasKey(host, identity);
    withAliasClaim(root.base, key, () => publishAlias(root, key), root);
  } catch (error) {
    throw protectionUnavailable(error);
  }
}

/** Only a verified new-session declaration may call this atomic initializer. */
export function establishProtectionAlias(host, identity, { base = defaultBase() } = {}) {
  try {
    const key = aliasKey(host, identity);
    if (typeof base !== "string" || !path.isAbsolute(base)) throw new SessionProtectionUnavailable();
    if (process.platform === "win32") {
      return Object.freeze({ base, id: windowsSessionProtection("establish", { base, key }) });
    }
    fs.mkdirSync(base, { recursive: true, mode: 0o700 });
    privateDirectory(base);
    return withAliasClaim(base, key, () => {
      const existing = findProtectionAlias(host, identity, { base });
      if (existing) return existing;
      const root = createProtectionRoot({ base });
      publishAlias(root, key);
      return root;
    });
  } catch (error) {
    throw protectionUnavailable(error);
  }
}

/** A missing alias is distinct from a damaged record; neither creates state. */
export function findProtectionAlias(host, identity, { base = defaultBase() } = {}) {
  try {
    const key = aliasKey(host, identity);
    if (process.platform === "win32") {
      const id = windowsSessionProtection("find", { base, key });
      return id === null ? undefined : Object.freeze({ base, id });
    }
    let id;
    try {
      const directory = aliasDirectory(base);
      id = readAlias(path.join(directory, key + ".json"), key);
    } catch (error) {
      if (error?.code === "ENOENT") return undefined;
      throw error;
    }
    const root = Object.freeze({ base, id });
    location(root);
    return root;
  } catch (error) {
    throw protectionUnavailable(error);
  }
}

/** Resume resolves existing state only; absent history never initializes clean state. */
export function protectionForAlias(host, identity, options) {
  const root = findProtectionAlias(host, identity, options);
  if (!root) throw new SessionProtectionUnavailable();
  return root;
}
