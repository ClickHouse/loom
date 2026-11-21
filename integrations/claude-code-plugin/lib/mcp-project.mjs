// SessionStart knows the checkout; headersHelper runs in the plugin directory.
// Match their common process ancestor by PID and creation time, never shell text.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readJsonFile, tmpStatePath, writeJsonFile } from "./tmp-state.mjs";

const SHELLS = new Set(["sh", "dash", "bash", "zsh", "busybox", "cmd.exe", "powershell.exe", "pwsh.exe", "pwsh"]);
const MAX_ANCESTORS = 3;
const LOOKUP_MS = 1500;

/** One bounded OS query. An unavailable identity supplies no implicit scope. */
export function processIdentity(
  pid,
  { platform = process.platform, run = execFileSync, read = fs.readFileSync, env = process.env } = {},
) {
  if (!Number.isSafeInteger(pid) || pid < 2) return null;
  try {
    if (platform === "linux") {
      const stat = read(`/proc/${pid}/stat`, "utf8");
      const fields = stat
        .slice(stat.lastIndexOf(") ") + 2)
        .trim()
        .split(/\s+/);
      const boot = read("/proc/sys/kernel/random/boot_id", "utf8").trim();
      if (!/^\d+$/.test(fields[1] || "") || !/^\d+$/.test(fields[19] || "") || !boot) return null;
      return {
        pid,
        parent: Number(fields[1]),
        born: `${boot}:${fields[19]}`,
        name: stat.slice(stat.indexOf("(") + 1, stat.lastIndexOf(") ")),
      };
    }
    const opts = {
      encoding: "utf8",
      timeout: 1000,
      windowsHide: true,
      maxBuffer: 16384,
      env: { ...env, LC_ALL: "C", TZ: "UTC" },
    };
    if (platform === "darwin") {
      const output = run("ps", ["-p", String(pid), "-o", "ppid=", "-o", "lstart=", "-o", "ucomm="], opts).trim();
      const match = /^(\d+)\s+(\w{3}\s+\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(\S+)$/.exec(output);
      return match ? { pid, parent: Number(match[1]), born: match[2], name: match[3] } : null;
    }
    if (platform === "win32") {
      if (pid > 4294967295) return null;
      const root = env.SystemRoot;
      if (typeof root !== "string" || !/^[A-Za-z]:[\\/]/.test(root)) return null;
      const host = path.win32.join(root, "System32", "cscript.exe");
      const helper = fileURLToPath(new URL("../assets/mcp-project-win32.js", import.meta.url));
      const output = run(host, ["//nologo", "//E:JScript", "//B", helper, String(pid)], opts);
      const match = /^((?:[A-Za-z0-9_.!~*'()-]|%[0-9A-F]{2})+)\t(0|[1-9][0-9]{0,9})\t([1-9][0-9]{0,19})\r?\n?$/.exec(
        output,
      );
      if (!match || match[0] !== output) return null;
      const parent = Number(match[2]);
      if (!Number.isSafeInteger(parent) || parent > 4294967295) return null;
      const name = decodeURIComponent(match[1]);
      return name ? { pid, parent, born: match[3], name } : null;
    }
  } catch {
    // Missing OS tooling or an exited process must not select another session.
  }
  return null;
}

function contextPath(identity, tmp) {
  const owner = typeof process.getuid === "function" ? process.getuid() : os.homedir();
  return tmpStatePath("mcp-project", `${owner}|${identity.pid}|${identity.born}`, { tmp });
}

/** The hook is a direct child of the client. Written before any network work. */
export function publishProject(payload, { pid = process.ppid, identity = processIdentity, tmp } = {}) {
  const parent = identity(pid);
  if (!parent?.born || !path.isAbsolute(String(payload.cwd || "")) || !payload.session_id) return false;
  return writeJsonFile(contextPath(parent, tmp), { identity: parent, cwd: payload.cwd, session: payload.session_id });
}

/** A helper may sit below a shell. Only the nearest client above known shells can match.
 *  Lookup misses keep the existing behavior: no implicit checkout scope. */
export async function sessionProject({ pid = process.ppid, identity = processIdentity, tmp, waitMs = LOOKUP_MS } = {}) {
  const deadline = Date.now() + waitMs;
  const ancestors = [];
  for (let n = 0; n < MAX_ANCESTORS && pid > 1; n++) {
    const current = identity(pid);
    if (!current?.born || !Number.isSafeInteger(current.parent) || current.parent === current.pid) break;
    if (!SHELLS.has(String(current.name || "").toLowerCase())) {
      ancestors.push(current);
      break;
    }
    pid = current.parent;
    if (waitMs > 0 && Date.now() >= deadline) break;
  }
  if (ancestors.length === 0) return "";
  do {
    for (const ancestor of ancestors) {
      const context = readJsonFile(contextPath(ancestor, tmp), null);
      if (
        context?.identity?.born === ancestor.born &&
        context.identity.pid === ancestor.pid &&
        path.isAbsolute(String(context.cwd || "")) &&
        context.session
      )
        return context.cwd;
    }
    if (Date.now() >= deadline) return "";
    await new Promise((resolve) => setTimeout(resolve, Math.min(25, deadline - Date.now())));
  } while (true);
}
