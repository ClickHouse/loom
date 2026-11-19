// Durable, content-free state shared by one session lineage. Unlike hook caches,
// this state has no expiry, reset or best-effort write after private output.

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { readPrivateFile } from "./tmp-state.mjs";

const ROOT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const VERSION = 1;
const defaultBase = () => path.join(os.homedir(), ".loom", "session-protection", "v1");

export class SessionProtectionUnavailable extends Error {
  constructor() {
    super("Session protection state could not be confirmed");
    this.name = "SessionProtectionUnavailable";
  }
}

function privateDirectory(directory) {
  // POSIX mode bits do not establish Windows DACL authority.
  if (process.platform === "win32" || typeof process.getuid !== "function") throw new SessionProtectionUnavailable();
  const st = fs.lstatSync(directory);
  if (!st.isDirectory() || st.isSymbolicLink() || (st.mode & 0o077) !== 0) throw new SessionProtectionUnavailable();
  if (typeof process.getuid === "function" && st.uid !== process.getuid()) throw new SessionProtectionUnavailable();
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
  } catch {
    throw new SessionProtectionUnavailable();
  }
}

/** Unknown, damaged or inaccessible state suppresses automatic sends. */
export function sessionIsProtected(root) {
  try {
    const directory = location(root);
    return privateText(path.join(directory, "state")) !== "0\n";
  } catch {
    return true;
  }
}

/** Complete this durable write before exposing a mirrored response to its caller. */
export function protectSession(root) {
  try {
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
  } catch {
    throw new SessionProtectionUnavailable();
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

function readAlias(file, key) {
  const record = JSON.parse(privateText(file));
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

/** Bind only a verified host identity; an existing alias can never choose a new root. */
export function bindProtectionAlias(root, host, identity) {
  try {
    location(root);
    const key = aliasKey(host, identity);
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
  } catch {
    throw new SessionProtectionUnavailable();
  }
}

/** A missing alias is distinct from a damaged record; neither creates state. */
export function findProtectionAlias(host, identity, { base = defaultBase() } = {}) {
  try {
    const key = aliasKey(host, identity);
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
  } catch {
    throw new SessionProtectionUnavailable();
  }
}

/** Resume resolves existing state only; absent history never initializes clean state. */
export function protectionForAlias(host, identity, options) {
  const root = findProtectionAlias(host, identity, options);
  if (!root) throw new SessionProtectionUnavailable();
  return root;
}
