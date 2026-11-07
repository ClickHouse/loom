// The plugin's shared client: settings, credentials and scope, the host
// payload, tool-shape normalization, and the one bounded transport.
//
// Zero dependencies, Node >= 18 (global fetch, AbortSignal.timeout): this
// runs inside the user's coding-agent environment and must start at once.
// A hook runs on the agent's critical path against a remote server, so no
// request may outlast its bound and nothing here throws.

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

// --- bounds -------------------------------------------------------------------
// The request bound comes from the round trip SessionStart measured against
// this server (5× rtt, clamped to 3,000–8,000 ms; lib/hook-bounds.mjs), so a
// slow link gets room and a LAN client keeps the floor. LOOM_HOOK_TIMEOUT_MS
// overrides it (`loom doctor --client` flags that). The hard-exit grace
// equals the bound: the unref'd timer only fires when a request is slower
// than that, and a shorter grace would kill a fire-and-forget post before it
// left a slow link.
import { DEFAULT_BOUND_MS, boundMs, rttCachePath } from "./hook-bounds.mjs";
import { readJsonFile, tmpStatePath, writeJsonFile } from "./tmp-state.mjs";

let _rttCache;
function readRttCache() {
  if (_rttCache === undefined) _rttCache = readJsonFile(rttCachePath(tmpdir(), resolveServerUrl()), null);
  return _rttCache;
}

/** The request abort bound for this process (ms). */
export function requestTimeoutMs() {
  return boundMs({ envValue: process.env.LOOM_HOOK_TIMEOUT_MS, cache: readRttCache() }).ms;
}

/** The hard exit grace for this process (ms): the request bound, unless
 *  LOOM_HOOK_EXIT_MS says otherwise. */
export function exitGraceMs() {
  const env = process.env.LOOM_HOOK_EXIT_MS || process.env.LOOM_HOOK_TIMEOUT_MS;
  return boundMs({ envValue: env, cache: readRttCache() }).ms;
}

/** Record one measured round trip for `serverUrl` (SessionStart). */
export function recordRoundTrip(serverUrl, rttMs) {
  // A failed write leaves the default bound standing.
  writeJsonFile(rttCachePath(tmpdir(), serverUrl), { rtt_ms: Math.round(Number(rttMs)), ts: Date.now() });
}

/** One `GET /v1/health` (SessionStart): `ms` is the round trip when it
 *  answered 2xx, else null; `body` is the parsed answer, or null. */
export async function probeHealth(serverUrl) {
  const started = Date.now();
  // Waited for beside the primer, so a lower LOOM_HOOK_TIMEOUT_MS bounds it too.
  const timeoutMs = Math.min(DEFAULT_BOUND_MS, requestTimeoutMs());
  const res = await requestJson(`${serverUrl}/v1/health`, { method: "GET", timeoutMs });
  const ok = res.status >= 200 && res.status < 300;
  return { ms: ok ? Date.now() - started : null, body: ok ? res.body : null };
}

// --- switches and numbers from the environment ------------------------------
const ON_WORDS = new Set(["on", "true", "1", "yes"]);
const OFF_WORDS = new Set(["off", "false", "0", "no"]);

/** A boolean setting: on/true/1/yes or off/false/0/no, any case; unset or
 *  anything else is `fallback`. */
export function envSwitch(name, fallback, env = process.env) {
  const v = String(env[name] ?? "")
    .trim()
    .toLowerCase();
  if (ON_WORDS.has(v)) return true;
  if (OFF_WORDS.has(v)) return false;
  return fallback;
}

/** A numeric setting clamped to [min, max]; unset, empty or not a number
 *  is `fallback`. */
export function envNumber(name, fallback, { min = -Infinity, max = Infinity, env = process.env } = {}) {
  const raw = String(env[name] ?? "").trim();
  const n = raw === "" ? Number.NaN : Number(raw);
  return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback;
}

// --- auth + scoping ----------------------------------------------------------

// File-based config for a hosted/remote Loom: ~/.loom/config.json may hold
// { url, token, namespace, org } so a user points the plugin at their server
// with ONE file — no shell exports in ~/.zshrc. Env vars still take priority
// (overrides / CI); this is the file-based default beneath them. Read once
// (hook processes are short-lived) and never throws.
/** ~/.loom/config.json, or LOOM_CONFIG_FILE. */
export function loomConfigPath(env = process.env) {
  return env.LOOM_CONFIG_FILE || join(homedir(), ".loom", "config.json");
}

/** ~/.loom/token, or LOOM_API_KEY_FILE. */
export function loomTokenPath(env = process.env) {
  return env.LOOM_API_KEY_FILE || join(homedir(), ".loom", "token");
}

/** The config object in `path`; {} when it is missing or not a JSON object. */
export function readConfigFile(path) {
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** Write `text` to `path` atomically, the file 0600 and a new directory 0700.
 *  Throws on failure; the caller decides what a failed write means. */
export function writePrivateFile(path, text) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const tmp = `${path}.${process.pid}.tmp`;
  writeFileSync(tmp, text, { mode: 0o600 });
  renameSync(tmp, path);
  chmodSync(path, 0o600);
}

/** Merge `patch` into the config file at `path` (created 0600 if absent). */
export function writeConfigMerged(path, patch) {
  const next = { ...readConfigFile(path), ...patch };
  writePrivateFile(path, JSON.stringify(next, null, 2) + "\n");
  return next;
}

// Read once per process (hooks are short-lived).
let _loomConfigCache;
export function loomConfig() {
  if (_loomConfigCache === undefined) _loomConfigCache = readConfigFile(loomConfigPath());
  return _loomConfigCache;
}

/** The Loom bearer token and the lookup that supplied it (`kind`: "env",
 *  "agent", "option", "config", "file", or "" for none), in this order: env,
 *  an agent's exchanged token, the install option, ~/.loom/config.json,
 *  ~/.loom/token. */
export function tokenLookup() {
  const fromEnv = (process.env.LOOM_API_KEY || "").trim();
  if (fromEnv) return { token: fromEnv, kind: "env" };
  // An agent signs in as itself or not at all: never as a credential a person left here.
  if (agentMode()) {
    const exchanged = agentToken();
    return { token: exchanged, kind: exchanged ? "agent" : "" };
  }
  // The plugin's install-time option: Claude Code exports
  // `userConfig` values to hook processes as CLAUDE_PLUGIN_OPTION_<KEY>.
  const fromOption = (process.env.CLAUDE_PLUGIN_OPTION_TOKEN || "").trim();
  if (fromOption) return { token: fromOption, kind: "option" };
  const fromCfg = String(loomConfig().token || "").trim();
  if (fromCfg) return { token: fromCfg, kind: "config" };
  try {
    const fromFile = readFileSync(loomTokenPath(), "utf8").trim();
    if (fromFile) return { token: fromFile, kind: "file" };
  } catch {
    // no token file; caller decides whether to no-op
  }
  return { token: "", kind: "" };
}

export function resolveToken() {
  return tokenLookup().token;
}

// --- an agent with no Loom token of its own ------------------------------------
// LOOM_CLIENT_ID names a workload trust (`loom bot create --agent-claim`), and
// LOOM_WORKLOAD_TOKEN or LOOM_WORKLOAD_TOKEN_FILE holds the OIDC token the
// agent's control plane issued it. bin/loom-agent-token.mjs exchanges that for
// a Loom token (RFC 8693; it never outlives the workload token) and saves it
// here, so nothing long-lived sits on the agent's machine.

export function agentMode(env = process.env) {
  return Boolean(env.LOOM_CLIENT_ID && (env.LOOM_WORKLOAD_TOKEN || env.LOOM_WORKLOAD_TOKEN_FILE));
}

/** The workload token as the agent holds it now; "" when unreadable. */
export function workloadToken(env = process.env) {
  try {
    return (env.LOOM_WORKLOAD_TOKEN || readFileSync(env.LOOM_WORKLOAD_TOKEN_FILE, "utf8")).trim();
  } catch {
    return "";
  }
}

export function agentTokenPath(env = process.env) {
  const key = createHash("sha256").update(String(env.LOOM_CLIENT_ID)).digest("hex").slice(0, 16);
  return join(env.HOME || homedir(), ".loom", `agent-token-${key}.json`);
}

/** The saved token is bound to the workload token it was exchanged for, so it
 *  serves only the agent holding that proof: one sharing this home, or one
 *  handed another agent's token, never gets it. A new proof is a new exchange
 *  (the Loom token never outlives the proof anyway). */
export function proofHash(env = process.env, token = workloadToken(env)) {
  return createHash("sha256").update(token).digest("hex");
}

// Renewed halfway through its life, at most five minutes before it runs out
// (a workload token may live only minutes), and used to its last half-minute
// while Loom is away; a failed renewal is not tried again for half a minute.
const AGENT_RENEW_S = 300;
const AGENT_LAST_S = 30;
const AGENT_RETRY_S = 30;

function usable(saved, env, now, minLeft) {
  const ours =
    saved.client_id === env.LOOM_CLIENT_ID && saved.server === resolveServerUrl() && saved.proof === proofHash(env);
  return ours && Number(saved.exp) - now > minLeft && Boolean(saved.token);
}

function renewDue(saved) {
  return Math.min(AGENT_RENEW_S, Number(saved.ttl) / 2 || AGENT_RENEW_S);
}

/** In agent mode, the saved token, renewed first when it is due. */
export function agentToken({ env = process.env, renew = renewAgentToken, now = Date.now() / 1000 } = {}) {
  if (!agentMode(env)) return "";
  const path = agentTokenPath(env);
  let saved = readConfigFile(path);
  if (usable(saved, env, now, renewDue(saved))) return String(saved.token);
  // The retry mark names the proof that failed, never the saved token's.
  if (!(Number(saved.retry_at) > now && saved.retry_proof === proofHash(env))) {
    renew(env);
    saved = readConfigFile(path);
    if (usable(saved, env, now, renewDue(saved))) return String(saved.token);
    try {
      writePrivateFile(path, JSON.stringify({ ...saved, retry_proof: proofHash(env), retry_at: now + AGENT_RETRY_S }));
    } catch {
      // nowhere to note it: the next call tries again
    }
  }
  return usable(saved, env, now, AGENT_LAST_S) ? String(saved.token) : "";
}

function renewAgentToken(env) {
  // A child, so every caller stays synchronous; once per half-life of the token.
  // A path, not the URL's pathname: that is percent-encoded (a space) and,
  // on Windows, starts with a slash before the drive.
  const script = fileURLToPath(new URL("../bin/loom-agent-token.mjs", import.meta.url));
  try {
    execFileSync(process.execPath, [script], { env, timeout: 3000, stdio: "ignore", windowsHide: true });
  } catch {
    // Loom away or the workload token refused: what is saved still serves.
  }
}

// A plugin may ship `plugin-defaults.json` at its root
// ({"server_url": "https://…"}) — the server its bundled MCP entry points at.
// The Codex plugin does (Codex has no install-time option to ask for a URL);
// the Claude Code plugin does not. Read once, never throws.
let _pluginDefaultsCache;
export function pluginDefaults(path = new URL("../plugin-defaults.json", import.meta.url)) {
  if (_pluginDefaultsCache !== undefined) return _pluginDefaultsCache;
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    _pluginDefaultsCache = parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    _pluginDefaultsCache = {};
  }
  return _pluginDefaultsCache;
}

/** The repository whose code index the session uses when one is named:
 *  LOOM_CODE_REPO, the plugin's `code_repo` option (Claude Code exports it as
 *  CLAUDE_PLUGIN_OPTION_CODE_REPO), then `code_repo` in ~/.loom/config.json
 *  (the one place a Codex user can set it). `owner/name` or a remote URL; ""
 *  when none, and the checkout's own remotes decide. It is what lets a
 *  session outside any git checkout use a repository's index. */
export function resolveCodeRepo(env = process.env) {
  return String(env.LOOM_CODE_REPO || env.CLAUDE_PLUGIN_OPTION_CODE_REPO || loomConfig().code_repo || "").trim();
}

/** A base URL as the hooks compare and send it: trimmed, no trailing slash. */
export function normalizeUrl(url) {
  return String(url || "")
    .trim()
    .replace(/\/+$/, "");
}

/** Base URL of the Loom control plane: env, then the plugin's install-time
 *  option, then ~/.loom/config.json, then the plugin's shipped
 *  default, then localhost. */
export function resolveServerUrl() {
  return normalizeUrl(
    process.env.LOOM_BASE_URL ||
      process.env.CLAUDE_PLUGIN_OPTION_SERVER_URL ||
      loomConfig().url ||
      pluginDefaults().server_url ||
      "http://localhost:7777",
  );
}

/**
 * Best-effort decode of a JWT's claims — read-only, NO signature check (the
 * server verifies; this is just to read org/namespace so the user doesn't
 * have to re-declare identity the token already carries). Returns {} on any
 * malformed input.
 */
export function decodeTokenClaims(token) {
  try {
    const part = (token || "").split(".")[1];
    if (!part) return {};
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64.padEnd(Math.ceil(b64.length / 4) * 4, "=");
    return JSON.parse(Buffer.from(padded, "base64").toString("utf8")) || {};
  } catch {
    return {};
  }
}

/**
 * Project-scoping resolver. The git-toplevel basename becomes the project
 * name; sibling repos stay isolated. Precedence:
 *   1. LOOM_PROJECT_NAME (explicit override)
 *   2. `git rev-parse --show-toplevel` basename
 *   3. cwd basename
 * Bounded git call — never blocks the hook for more than ~1s.
 */
export function resolveProject(cwd, { top } = {}) {
  const dir = dirOf(cwd);
  const override = (process.env.LOOM_PROJECT_NAME || "").trim();
  if (override) return override;
  const root = top === undefined ? gitToplevel(dir) : top;
  return basename(gitSpelling.get(root) || root || dir) || "default";
}

// --- the repository's stable identity -------------------------
// The project name is the checkout's folder name: two clones of different
// repositories both called `app` share it, and a renamed clone forks its own
// history. The git remote is what stays the same across clones and renames,
// so every capture also carries `repo:<10 hex of sha256(normalised remote)>`
// and the readers can scope on it. The normalisation folds every URL shape of
// one repository onto one string: scheme, user, `.git`, trailing slash and
// case are dropped; ssh `host:owner/name` and https `host/owner/name` agree.

/** {id, slug, canonical} for a git remote URL, or null when it has no host/owner/name. */
export function repoIdentity(remoteUrl) {
  let u = String(remoteUrl || "").trim();
  if (!u) return null;
  u = u.replace(/^[a-z+]+:\/\//i, ""); // https://, ssh://, git+ssh://
  u = u.replace(/^[^@/]+@/, ""); // git@ / user@
  u = u.replace(/:(\d+)?\//, "/"); // host:port/ → host/ ; host:owner → host/owner
  u = u.replace(/:/, "/");
  u = u
    .replace(/\/+$/, "")
    .replace(/\.git$/i, "")
    .replace(/\/+$/, "")
    .toLowerCase();
  const parts = u.split("/").filter(Boolean);
  if (parts.length < 3) return null;
  const canonical = parts.slice(0, 3).join("/");
  const id = createHash("sha256").update(canonical).digest("hex").slice(0, 10);
  return { id, slug: `${parts[1]}/${parts[2]}`, canonical };
}

/** A payload's `cwd` when it is a non-empty string, else this process's. */
function dirOf(cwd) {
  return typeof cwd === "string" && cwd.trim() ? cwd : process.cwd();
}

/** One bounded git call's stdout, trimmed unless `raw` (a diff or porcelain
 *  output keeps its bytes); "" when git is missing, the directory is not a
 *  repository, or the call fails, times out or writes past `maxBuffer`. */
export function git(args, cwd, { timeoutMs = 1500, raw = false, maxBuffer = 16 * 1024 * 1024 } = {}) {
  try {
    const out = execFileSync("git", args, {
      cwd: dirOf(cwd),
      encoding: "utf8",
      timeout: timeoutMs,
      maxBuffer,
      stdio: ["ignore", "pipe", "ignore"],
      windowsHide: true,
    });
    return raw ? out : out.trim();
  } catch {
    return "";
  }
}

/** `p` as the file system names it: symlinks resolved and, on Windows,
 *  long names, drive-letter case and backslashes. Two spellings of one place
 *  (macOS /tmp and /private/tmp, a Windows 8.3 temp path, git's C:/ form)
 *  come out equal. The part of `p` that does not exist yet is kept as given. */
export function realPath(p) {
  let head = resolve(String(p));
  const rest = [];
  for (;;) {
    try {
      return join(realpathSync.native(head), ...rest);
    } catch {
      const up = dirname(head);
      if (up === head) return resolve(String(p));
      rest.unshift(basename(head));
      head = up;
    }
  }
}

// A hook asks about one checkout root many times (each edit a transcript
// names): it is resolved once.
let lastRoot = "";
let lastRealRoot = "";
function realRoot(root) {
  if (root !== lastRoot) {
    lastRealRoot = realPath(root);
    lastRoot = root;
  }
  return lastRealRoot;
}

function isDirectory(p) {
  try {
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function isSymlink(p) {
  try {
    return lstatSync(p).isSymbolicLink();
  } catch {
    return false;
  }
}

/** `p` relative to `root` with `/` separators ("" for the root itself), or
 *  null when `p` lies outside it. Both are compared as realPath names them,
 *  so a payload path and git's toplevel agree however each is spelled; when
 *  `p` itself is a symlink to a file its own name is kept, so one in the
 *  checkout (a virtualenv's bin/python) stays where it is. */
export function pathUnder(root, p) {
  if (!root || !p) return null;
  const abs = resolve(String(p));
  const keepName = isSymlink(abs) && !isDirectory(abs);
  const rel = relative(realRoot(root), keepName ? join(realPath(dirname(abs)), basename(abs)) : realPath(abs));
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) return null;
  return rel.split(sep).join("/");
}

// The toplevel as git printed it, by its realPath: the project name is that
// folder's name as git spells it, so a changed spelling (letter case, an 8.3
// name on Windows) never moves a project to another namespace.
const gitSpelling = new Map();

/** The checkout root for `cwd`, as realPath names it; "" outside a
 *  repository. */
export function gitToplevel(cwd) {
  const top = git(["rev-parse", "--show-toplevel"], cwd);
  if (!top) return "";
  const real = realPath(top);
  gitSpelling.set(real, top);
  return real;
}

/** The per-checkout repository cache every hook shares (lib/repo-scope.mjs
 *  reads and writes it): $TMPDIR/loom-shunt-<sha1(root)[0:12]>.json. A root
 *  path is keyed as realPath names it, so every spelling of one checkout
 *  shares the entry. */
export function shuntCachePath(root, tmp = tmpdir()) {
  const key = isAbsolute(String(root)) ? realPath(root) : String(root);
  return tmpStatePath("shunt", key, { tmp });
}

/** The remote URL of the repository the checkout's index
 *  resolved to, from the shared cache (no network call): the remote that
 *  matched (`upstream`, `remote:<name>`), or the GitHub upstream of a fork
 *  (`fork_of`); `origin` for an origin or override match, or when the entry names
 *  none. "" when
 *  the cache holds no indexed entry. */
function indexedRemoteUrl(dir, tmp, root) {
  if (!root) return "";
  const c = readJsonFile(shuntCachePath(root, tmp), null);
  if (!c || c.indexed !== true || !c.repo) return "";
  const via = String(c.matchedVia || "origin");
  if (via === "fork_of") return `https://github.com/${c.repo}`;
  const name = via === "upstream" ? "upstream" : via.startsWith("remote:") ? via.slice(7) : "origin";
  return git(["config", "--get", `remote.${name}.url`], dir);
}

/** The URL the checkout's repository identity comes from ("" when not a repo
 *  / no remote); bounded. `LOOM_REPO_REMOTE` wins; then the remote the
 *  shared index resolution matched (a fork's upstream); else
 *  the `upstream` remote over `origin`, so a fork and an upstream checkout
 *  tag their memories with one `repo:<id>`. */
export function resolveRemote(cwd, { tmp = tmpdir(), top } = {}) {
  const override = (process.env.LOOM_REPO_REMOTE || "").trim();
  if (override) return override;
  const dir = dirOf(cwd);
  return (
    indexedRemoteUrl(dir, tmp, top === undefined ? gitToplevel(dir) : top) ||
    git(["config", "--get", "remote.upstream.url"], dir) ||
    git(["config", "--get", "remote.origin.url"], dir)
  );
}

/** cwd relative to the git toplevel ("" at the root or outside a repo). */
export function resolveRelDir(cwd, { top } = {}) {
  const dir = dirOf(cwd);
  const root = top === undefined ? gitToplevel(dir) : top;
  if (!root) return "";
  return pathUnder(root, dir) ?? "";
}

/**
 * How the project maps onto Loom's identity tuple. Loom's governance
 * (org/namespace/agent) is unchanged; this only adds the per-repo
 * convenience scoping the coding agent gets for free.
 *
 * LOOM_PROJECT_SCOPE:
 *   "tag"       (default) — one shared namespace, project carried as a tag.
 *   "namespace" — project basename becomes the namespace itself.
 *
 * `repo` ({id, slug} from the remote, or null) and `relDir` ride along in
 * both modes; the `repo:<id>` tag joins the project tag in tag mode.
 */
export function resolveScope(cwd) {
  const top = gitToplevel(cwd); // once: project, remote and relDir all need it
  const project = resolveProject(cwd, { top });
  const repo = repoIdentity(resolveRemote(cwd, { top }));
  const relDir = resolveRelDir(cwd, { top });
  const mode = (process.env.LOOM_PROJECT_SCOPE || "tag").trim();
  // Identity defaults derive from the token's own claims, so a user only
  // needs a token — no LOOM_ORG / LOOM_NAMESPACE to export. Explicit env
  // still wins. A wildcard ('*') namespace claim is skipped (not a real
  // workspace) so we fall back to the 'coding' default.
  const claims = decodeTokenClaims(resolveToken());
  const claimNs = Array.isArray(claims.namespaces) ? claims.namespaces.find((n) => n && n !== "*") : "";
  // An agent's identity is its own token's: never the scope a person saved here.
  const cfg = agentMode() ? {} : loomConfig();
  const org = (process.env.LOOM_ORG || String(cfg.org || "") || claims.org || "default").trim();
  const agent = (process.env.LOOM_AGENT || String(cfg.agent || "") || hookClient()).trim();
  const baseNs = (process.env.LOOM_NAMESPACE || String(cfg.namespace || "") || claimNs || "coding").trim();
  if (mode === "namespace") {
    return { org, namespace: project, agent, project, repo, relDir, tags: repo ? [`repo:${repo.id}`] : [] };
  }
  const tags = [`project:${project}`];
  if (repo) tags.push(`repo:${repo.id}`);
  return { org, namespace: baseNs, agent, project, repo, relDir, tags };
}

// --- which coding agent is running the hook ----------------------------------
// The Claude Code and Codex plugins ship these scripts byte for byte; each
// plugin's hooks.json names its client as the hook's third argument
// (`node loom-capture.mjs PostToolUse codex`). The name becomes the default
// agent, the run-id prefix, the `source:` tag and the consumer label, so the
// server can tell the two apart without either plugin carrying its own copy
// of the scripts. LOOM_CLIENT is the env-var form for
// hand-wired clients; an unnamed client is Claude Code.
const _CLIENT_RE = /^[a-z][a-z0-9-]{1,31}$/;
const _RUN_PREFIX = { "claude-code": "cc", codex: "cx" };

export function hookClient(argv = process.argv, env = process.env) {
  const fromArgv = String(argv[3] || "")
    .trim()
    .toLowerCase();
  if (_CLIENT_RE.test(fromArgv)) return fromArgv;
  const fromEnv = String(env.LOOM_CLIENT || "")
    .trim()
    .toLowerCase();
  if (_CLIENT_RE.test(fromEnv)) return fromEnv;
  return "claude-code";
}

/** The run-id prefix for a client: `cc-<session>` for Claude Code,
 *  `cx-<session>` for Codex, `cc-` for anything else. */
export function runPrefixFor(client) {
  return _RUN_PREFIX[String(client || "")] || "cc";
}

// --- tool-name and tool-input shapes across clients --------------------------
// Claude Code names its tools Read / Grep / Bash / Edit / Write and gives a
// string `command`. Codex runs everything through a shell tool whose input
// may be an argv array (`["bash", "-lc", "cat x"]`) and edits files through
// `apply_patch`, whose input is the patch text. These helpers give the
// heuristics one shape to reason about.
const _SHELL_TOOLS = new Set(["shell", "exec_command", "local_shell", "container.exec", "shell_command"]);

/** A client's shell tool name folded onto "Bash"; other names pass through. */
export function normalizeToolName(name) {
  const n = String(name || "").trim();
  return _SHELL_TOOLS.has(n) ? "Bash" : n;
}

/** The shell command text of a tool input: a string as is; an argv array
 *  joined, with a leading `sh -c` / `bash -lc` wrapper stripped so the
 *  heuristics see the command the agent meant. "" when there is none. */
export function commandText(toolInput) {
  const ti = toolInput && typeof toolInput === "object" ? toolInput : {};
  const c = ti.command !== undefined ? ti.command : ti.cmd;
  if (typeof c === "string") return c;
  if (!Array.isArray(c)) return "";
  const parts = c.map((x) => String(x));
  if (parts.length >= 3 && /^(ba|z|da|)sh$/.test(basename(parts[0])) && /^-l?c$/.test(parts[1])) {
    return parts.slice(2).join(" ");
  }
  return parts.join(" ");
}

/** File paths named by an apply_patch envelope (`*** Update File: path`,
 *  `*** Add File:`, `*** Delete File:`), in order, bounded. */
export function patchFiles(text) {
  const out = [];
  const re = /^\*\*\* (?:Update|Add|Delete) File: (.+)$/gm;
  let m;
  while ((m = re.exec(String(text || ""))) !== null && out.length < 16) out.push(m[1].trim());
  return out;
}

// --- heuristic typing (ZERO-TOKEN; no LLM on the capture path) ---------------
// Capture runs no model: type and file extraction are heuristics over the
// tool name and inputs. LLM extraction/embedding/conflict-detection
// is deferred to Loom's async worker / session-end batch, server-side.

const _EDIT_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit", "str_replace_editor", "apply_patch"]);
// PowerShell is Claude Code's shell tool on Windows without Git Bash.
const _RUN_TOOLS = new Set([
  "Bash",
  "BashOutput",
  "PowerShell",
  "Shell",
  "command_run",
  "shell",
  "exec_command",
  "local_shell",
]);
const _FETCH_TOOLS = new Set(["WebFetch", "WebSearch", "Fetch"]);
const _READ_TOOLS = new Set(["Read", "Grep", "Glob", "LS"]);

/** Map a tool name + error flag to an episodic action type. */
export function inferActionType(toolName, isError) {
  if (isError) return "error";
  const name = normalizeToolName(toolName);
  if (_EDIT_TOOLS.has(name)) return "file_edit";
  if (_RUN_TOOLS.has(name)) return "command_run";
  if (_FETCH_TOOLS.has(name)) return "web_fetch";
  if (_READ_TOOLS.has(name)) return "file_read";
  return "tool_action";
}

/** Pull file paths out of a tool-input object (best-effort, bounded). */
export function extractFiles(toolInput) {
  if (!toolInput || typeof toolInput !== "object") return [];
  const keys = ["file_path", "path", "filePath", "notebook_path", "file", "filename"];
  const out = new Set();
  for (const k of keys) {
    const v = toolInput[k];
    if (typeof v === "string" && v.trim()) out.add(v.trim());
  }
  // apply_patch: the paths are inside the patch text
  for (const k of ["input", "patch"]) {
    const v = toolInput[k];
    if (typeof v === "string" && v.includes("*** Begin Patch")) for (const p of patchFiles(v)) out.add(p);
  }
  // MultiEdit / batch shapes
  if (Array.isArray(toolInput.edits)) {
    for (const e of toolInput.edits) {
      if (e && typeof e.file_path === "string") out.add(e.file_path);
    }
  }
  return [...out].slice(0, 16);
}

/** Truncate a narrative to a bounded length (no token spend). */
/** Text of one content block. A text block is its text. An image block is a
 *  placeholder that keeps the pointer and drops the bytes — "[image: image/png]"
 *  or "[image: image/png https://…]" for a URL source — so the turn records that
 *  a picture was shown where it was shown (0.03–0.14% of coding-agent turns
 *  carry one; dropping them would be silent). Other kinds
 *  (tool_use, thinking, tool_result) are scaffolding and yield "". */
export function blockText(b) {
  if (!b || typeof b !== "object") return "";
  if (b.type === "text" && typeof b.text === "string") return b.text;
  if (b.type === "image") {
    const src = b.source && typeof b.source === "object" ? b.source : {};
    const mediaType = typeof src.media_type === "string" && src.media_type ? src.media_type : "image";
    const where = src.type === "url" && typeof src.url === "string" ? ` ${src.url}` : "";
    return `[image: ${mediaType}${where}]`;
  }
  return "";
}

/** The readable text of a message's content: a string as-is, an array as its
 *  blocks' text joined by newlines (image placeholders included). */
export function contentText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map(blockText)
    .map((t) => t.trim())
    .filter(Boolean)
    .join("\n");
}

/** Image blocks carried inside tool_result blocks — a Read of a PNG, a
 *  screenshot tool. Zero when the content holds none. */
export function toolResultImageCount(content) {
  if (!Array.isArray(content)) return 0;
  let n = 0;
  for (const b of content) {
    if (!b || b.type !== "tool_result" || !Array.isArray(b.content)) continue;
    n += b.content.filter((x) => x && x.type === "image").length;
  }
  return n;
}

/** Placeholder turn for tool results that returned images: the pointer that
 *  "an image came back here", as its own `tool` turn, or null when none did. */
export function toolImageTurn(content) {
  const n = toolResultImageCount(content);
  if (!n) return null;
  return { role: "tool", content: `[image: ${n} image result${n === 1 ? "" : "s"} returned by a tool]` };
}

export function truncate(text, max = 2000) {
  const s = typeof text === "string" ? text : JSON.stringify(text ?? "");
  if (s.length <= max) return s;
  return s.slice(0, max - 14).trimEnd() + "\n…[truncated]";
}

// --- transport ----------------------------------------------------------------
// Every request the plugin makes goes through requestJson: one bound, one
// header set, one error shape. Nothing here throws or rejects.

/**
 * One bounded JSON request: `{ status, body, error }`, status 0 when no
 * response arrived (refused, reset, timed out); body null when the response
 * was not JSON. The bound covers reading the body too.
 */
export async function requestJson(
  url,
  { method = "POST", body, token = "", timeoutMs = requestTimeoutMs(), headers = {}, fetchImpl = fetch } = {},
) {
  try {
    const res = await fetchImpl(url, {
      method,
      headers: {
        ...(body === undefined ? {} : { "content-type": "application/json" }),
        ...(token ? { authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    let parsed = null;
    try {
      parsed = await res.json();
    } catch {
      /* an error body may not be JSON */
    }
    return { status: res.status, body: parsed };
  } catch (e) {
    return { status: 0, body: null, error: String((e && e.message) || e) };
  }
}

function opUrl(opName, serverUrl) {
  return `${serverUrl || resolveServerUrl()}/v1/${opName}`;
}

/**
 * POST a wire op and RETURN IMMEDIATELY: the request is detached, bounded by
 * requestTimeoutMs() (or a tighter `timeoutMs`), and its outcome ignored. A
 * capture hook never awaits it; its unref'd exit timer (scheduleExit) keeps
 * a slow socket from holding the host.
 */
export function postFireAndForget(opName, body, { token, serverUrl, timeoutMs } = {}) {
  const bearer = token || resolveToken();
  if (!bearer) return; // no credential: do nothing
  const bound = Math.min(Number(timeoutMs) > 0 ? Number(timeoutMs) : requestTimeoutMs(), requestTimeoutMs());
  void requestJson(opUrl(opName, serverUrl), {
    body,
    token: bearer,
    timeoutMs: bound,
    headers: { "x-request-id": `loomhook-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` },
  });
}

/**
 * POST a wire op whose answer the caller needs (the primer, a gate's
 * verdict): the parsed body of a 2xx answer, else null. `timeoutMs` may
 * exceed the default for a caller that runs rarely (the repository lookup).
 */
export async function postBounded(opName, body, opts = {}) {
  const res = await postBoundedResult(opName, body, opts);
  return res.status >= 200 && res.status < 300 && res.body && typeof res.body === "object" ? res.body : null;
}

/** postBounded's answer with its status: 0 for no token, a timeout or a network error. */
export async function postBoundedResult(opName, body, { token, serverUrl, timeoutMs } = {}) {
  const bearer = token || resolveToken();
  if (!bearer) return { status: 0, body: null };
  const bound = Number(timeoutMs) > 0 ? Number(timeoutMs) : requestTimeoutMs();
  return requestJson(opUrl(opName, serverUrl), { body, token: bearer, timeoutMs: bound });
}

// --- diagnostics ------------------------------------------------------------

// Best effort, not a guarantee: common credential shapes only. A secret in
// another shape still reaches the server, whose blacklist and sensitivity
// sweep are the next line. Every open-ended run is bounded ({0,64} and the
// like): an unbounded one retried from each start is quadratic on a long
// tool output.
const PRIVATE_KEY_RE = /-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g;
const TOKEN_SHAPES_RE =
  /\b(?:(?:AKIA|ASIA)[0-9A-Z]{16}|(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abeprs]-[A-Za-z0-9-]{10,}|sk-(?:ant|proj)-[\w-]{20,}|glpat-[\w-]{20,})\b/g;
// `password=…`, `"api_key": "…"`, `AWS_SECRET_ACCESS_KEY=…`: the name stays, the value goes.
const SECRET_ASSIGNMENT_RE =
  /\b([\w.-]{0,64}?(?:password|passwd|secret|secret[_-]?key|token|api[_-]?key|access[_-]?key|private[_-]?key)\b["']?\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s"'\\,;}]+)/gi;
const JWT_RE = /\beyJ[\w-]{0,512}\.[\w-]{1,8192}\.[\w-]{0,1024}/g;
const JWT_AT = new RegExp(JWT_RE.source, "y");
const JWT_START = /\beyJ/g;
const WORD_RUN = /[\w-]*/y;

/** `s` with JWT_RE's matches replaced, in one pass. JWT_RE alone retries its
 *  header and payload scans from every `eyJ` in a long [\w-] run; but each
 *  part ends only where its run does, so each run is scanned once and a start
 *  is tried only when its header run ends in a dot within 512 characters and
 *  the payload run after it is 1 to 8192 characters ending in a dot. */
export function redactJwts(s) {
  let out = "";
  let last = 0;
  let runEnd = -1;
  let payloadOk = false;
  JWT_START.lastIndex = 0;
  for (let m = JWT_START.exec(s); m; m = JWT_START.exec(s)) {
    const i = m.index;
    if (i >= runEnd) {
      WORD_RUN.lastIndex = i + 3;
      WORD_RUN.exec(s);
      runEnd = WORD_RUN.lastIndex;
      // The payload is the next run: 1 to 8192 characters, then a dot.
      WORD_RUN.lastIndex = runEnd + 1;
      WORD_RUN.exec(s);
      const payload = WORD_RUN.lastIndex - runEnd - 1;
      payloadOk = s[runEnd] === "." && payload >= 1 && payload <= 8192 && s[WORD_RUN.lastIndex] === ".";
    }
    if (!payloadOk || runEnd - i - 3 > 512) continue;
    JWT_AT.lastIndex = i;
    const jwt = JWT_AT.exec(s);
    if (!jwt) continue; // the checks above imply a match; a pattern edit must not throw in a hook
    out += s.slice(last, i) + "[redacted]";
    last = i + jwt[0].length;
    JWT_START.lastIndex = last;
    runEnd = -1;
  }
  return out + s.slice(last);
}
const BEARER_RE = /\b(Bearer)\s+[^\s"'\\]+/gi;
const BASIC_AUTH_RE = /(\bAuthorization["']?\s*[:=]\s*["']?Basic)\s+[^\s"'\\]+/gi;
// `https://user:pass@host`: the scheme and host stay, the credentials go.
const URL_CREDENTIALS_RE = /\b([a-z][a-z0-9+.-]{0,31}:\/\/)[^\s/@"']{1,256}:[^\s/@"']{0,256}@/gi;

/** `replacement` in the quotes `value` had, so redacted JSON still parses. */
function quoted(value, replacement) {
  const q = value[0] === '"' || value[0] === "'" ? value[0] : "";
  return q + replacement + q;
}

/** `text` with private keys, cloud/GitHub/GitLab/Slack/OpenAI/Anthropic
 *  tokens, secret assignments, bearer and basic credentials, URL passwords,
 *  JWTs and the resolved token itself replaced. */
export function redactSecrets(text, token = resolveToken()) {
  let s = String(text ?? "");
  if (token && token.length >= 8) s = s.split(token).join("[redacted]");
  s = s
    .replace(PRIVATE_KEY_RE, "[redacted private key]")
    .replace(TOKEN_SHAPES_RE, "[redacted]")
    .replace(SECRET_ASSIGNMENT_RE, (_, name, value) => name + quoted(value, "[redacted]"));
  return redactJwts(s)
    .replace(BEARER_RE, "$1 [redacted]")
    .replace(BASIC_AUTH_RE, "$1 [redacted]")
    .replace(URL_CREDENTIALS_RE, "$1[redacted]@");
}

// Room past a cut for the longest bounded pattern (a JWT is under 10 KB), so
// a secret that straddles the cut is still matched whole.
const REDACT_MARGIN = 16 * 1024;

/** `redactSecrets` over the head of `text` its caller keeps (`max` chars, plus
 *  a margin): a multi-megabyte tool output is not scanned only to be cut. */
export function redactHead(text, max, token = resolveToken()) {
  const s = String(text ?? "");
  let cut = max + REDACT_MARGIN;
  // A secret split by the cut is not matched and stays, unredacted, at the
  // very end. Earlier redactions shorten the text and could pull it inside
  // `max`, so the last margin of the redacted text is always dropped; when
  // that leaves less than `max`, scan further (a few times at most).
  for (let tries = 0; tries < 4 && cut < s.length; tries++) {
    const out = redactSecrets(s.slice(0, cut), token);
    const keep = out.length - REDACT_MARGIN;
    if (keep >= max || tries === 3) return out.slice(0, Math.max(0, keep));
    cut += max - keep + REDACT_MARGIN;
  }
  return redactSecrets(s, token);
}

// What a hook's `git diff` passes so the user's diff settings cannot change what is
// sent: no external tool, colour or textconv program, git's own path prefixes, a
// submodule as one line rather than its commit subjects, and nearby hunks kept
// apart (`diff.interHunkContext` would merge them into one wider range).
export const DIFF_ARGS = [
  "--no-ext-diff",
  "--no-color",
  "--no-textconv",
  "--src-prefix=a/",
  "--dst-prefix=b/",
  "--submodule=short",
  "--inter-hunk-context=0",
];

// A hunk header: its ranges, then the function context git appends to it.
const HUNK_HEADER_RE = /^(@@+(?: [-+]\d+(?:,\d+)?)+ @@+)(.*)$/;
// TOKEN_SHAPES_RE without its global flag, so `test` keeps no position between calls.
const TOKEN_SHAPE_RE = new RegExp(TOKEN_SHAPES_RE.source);
const ACCESS_SPECIFIERS = new Set(["public:", "protected:", "private:"]);
const NOT_FUNCTION_NAMES = new Set(["if", "for", "while", "switch", "return", "catch", "sizeof", "decltype"]);
// Python's whitespace (`str.split()`, `str.strip()`) and line ends other than `\n`
// (`str.splitlines()`), so the client reads a header the way the server does.
const PY_SPACE = String.raw`\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000`;
const PY_SPACE_RE = new RegExp(`[${PY_SPACE}]+`);
const PY_TRIM_RE = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, "g");
const PY_LINE_END_RE = /[\r\v\f\x1c-\x1e\x85\u2028\u2029]/;
// Files whose hunk headers can name a function the code index holds. A hunk
// header elsewhere (Markdown, YAML, `.env`) quotes whatever line came before the
// change, so no name is sent for it. The list errs short: a code file left out
// (an extensionless script) loses only the name, and the server matches its hunks
// by line range.
const FUNCTION_FILE_RE =
  /\.(c|cc|cpp|cxx|h|hh|hpp|hxx|ipp|inl|py|pyi|pyw|ts|tsx|mts|cts|js|jsx|mjs|cjs|rs|go|java|vue|svelte|astro|proto|prisma|jsonnet|libsonnet|sh|bash|zsh|ksh|dash|mksh|bats|command)$/i;

/** The function name in a hunk header's context, as the server reads it
 *  (`_hunk_context_name` in server/code/app/symbol/diff.py): the last word before
 *  the first `(`, template arguments dropped, when it is a (scope-qualified)
 *  identifier. Empty when there is no such identifier, when the word before it ends
 *  in `=` or in a single `:` other than a C++ access specifier (`x = getpass(`,
 *  `Password: value (`), or when it has a credential's shape (`token ghp_… (`). */
export function hunkFunctionName(context) {
  const ctx = String(context ?? "").replace(PY_TRIM_RE, "");
  if (!ctx.includes("(")) return "";
  // two passes drop one level of nested template arguments, as the server does
  const head = ctx
    .slice(0, ctx.indexOf("("))
    .replace(/<[^<>]*>/g, "")
    .replace(/<[^<>]*>/g, "");
  const tokens = head.split(PY_SPACE_RE).filter(Boolean);
  if (!tokens.length) return "";
  const name = tokens[tokens.length - 1].replace(/^[&*]+|[&*]+$/g, "");
  const before = tokens[tokens.length - 2] ?? "";
  if (/(^|[^:]):$|=$/.test(before) && !ACCESS_SPECIFIERS.has(before)) return "";
  if (NOT_FUNCTION_NAMES.has(name)) return "";
  if (!/^~?[A-Za-z_][\p{L}\p{N}_]*(::~?[A-Za-z_][\p{L}\p{N}_]*)*$/u.test(name)) return "";
  // only the token shapes: SECRET_ASSIGNMENT_RE would read a `Secret::get` scope as an assignment
  return TOKEN_SHAPE_RE.test(name) ? "" : name;
}

/** The part of a unified diff that code.impact and code.test_gate read, at most
 *  `max` chars of whole lines: each file's `diff --git`, `---` and `+++` lines, and
 *  each hunk header's ranges with only the function name the server would take
 *  from its context (`@@ -1 +1 @@ name(`), for a code file. The changed lines, the
 *  rest of the context and the other header lines (an `index` line's blob hashes)
 *  are left out, so nothing in them is sent. */
export function diffHeaders(diff, max) {
  const out = [];
  let built = 0;
  let inFileHeader = false;
  let skipFile = false;
  let path = "";
  for (const line of String(diff ?? "").split("\n")) {
    let kept = null;
    if (line.startsWith("diff --git ")) {
      inFileHeader = true;
      skipFile = false;
    }
    const end = line.search(PY_LINE_END_RE);
    const hunk = HUNK_HEADER_RE.exec(end < 0 ? line : line.slice(0, end));
    // a file header line the server would split is dropped with the file's hunks,
    // which would otherwise be read as the previous file's
    if (inFileHeader && end >= 0 && !hunk) skipFile = true;
    if (skipFile) continue;
    if (hunk) {
      inFileHeader = false;
      const name = FUNCTION_FILE_RE.test(path) ? hunkFunctionName(hunk[2]) : "";
      kept = hunk[1] + (name ? ` ${name}(` : "");
    } else if (inFileHeader && /^(diff --git |--- |\+\+\+ )/.test(line)) {
      kept = line;
      if (line.startsWith("+++ ")) path = line.slice(4).trim();
    }
    if (kept === null) continue;
    const cost = (out.length ? 1 : 0) + kept.length;
    if (built + cost > max) {
      // a file header cut off from its hunks would make the server read the whole file
      while (out.length && !out[out.length - 1].startsWith("@@")) out.pop();
      break;
    }
    out.push(kept);
    built += cost;
  }
  return out.join("\n");
}

/**
 * Run a hook's `main`. Whatever it throws, the hook exits 0: a hook never
 * fails the host session. With LOOM_HOOK_DEBUG=on the error's first line,
 * redacted, goes to stderr, which is how a broken hook shows itself.
 */
export function runHookMain(main) {
  Promise.resolve()
    .then(main)
    .catch((e) => {
      if (envSwitch("LOOM_HOOK_DEBUG", false)) {
        const line = String((e && (e.stack || e.message)) || e).split("\n")[0];
        process.stderr.write(`loom hook ${basename(process.argv[1] || "")}: ${redactHead(line, 300).slice(0, 300)}\n`);
      }
      scheduleExit(0);
    });
}

/**
 * Schedule a hard, unref'd process exit. The timer does NOT keep the event
 * loop alive (unref), so the process exits naturally once fetch settles —
 * but if fetch hangs past the grace window, this guarantees termination.
 */
export function scheduleExit(code = 0, ms = exitGraceMs()) {
  setTimeout(() => process.exit(code), ms).unref();
}

// --- the host payload ------------------------------------------------------
// Claude Code's field names are canonical. Other clients spell a few of them
// differently (hooks.generic.json promises the scripts accept these), and a
// malformed client may send a number or an object where a string belongs.
// normalizePayload folds both onto the canonical names once, at the boundary.
const PAYLOAD_STRINGS = {
  hook_event_name: ["hook_event_name"],
  session_id: ["session_id", "sessionId"],
  transcript_path: ["transcript_path", "transcriptPath"],
  cwd: ["cwd"],
  prompt: ["prompt", "user_prompt"],
  tool_name: ["tool_name", "toolName", "tool"],
  last_assistant_message: ["last_assistant_message"],
  reason: ["reason"],
};
const PAYLOAD_VALUES = {
  tool_input: ["tool_input", "toolInput", "input"],
  tool_response: ["tool_response", "tool_result", "output"],
};

/** `raw` with every canonical string field a string (or absent) and the
 *  tool input/response under their canonical names. */
export function normalizePayload(raw) {
  const p = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const out = { ...p };
  for (const [key, names] of Object.entries(PAYLOAD_STRINGS)) {
    let v;
    for (const n of names) {
      const x = p[n];
      if (typeof x === "string" && x) v = x;
      else if (typeof x === "number" && Number.isFinite(x)) v = String(x);
      if (v !== undefined) break;
    }
    if (v === undefined) delete out[key];
    else out[key] = v;
  }
  for (const [key, names] of Object.entries(PAYLOAD_VALUES)) {
    const n = names.find((k) => p[k] !== undefined && p[k] !== null);
    if (n !== undefined) out[key] = p[n];
  }
  return out;
}

/** Read the host's hook payload (JSON on stdin), normalized. Resolves to a
 *  plain object: {} on EOF, on malformed JSON, on any non-object value and
 *  on a payload over MAX_STDIN_CHARS;
 *  gives up waiting after a second, so a host that never closes stdin
 *  cannot hold a hook open. */
const MAX_STDIN_CHARS = 32 * 1024 * 1024;

export function readStdinJson() {
  return new Promise((resolve) => {
    let raw = "";
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      try {
        resolve(normalizePayload(raw.trim() ? JSON.parse(raw) : {}));
      } catch {
        resolve({});
      }
    };
    try {
      process.stdin.setEncoding("utf8");
      process.stdin.on("data", (c) => {
        raw += c;
        if (raw.length > MAX_STDIN_CHARS) {
          raw = ""; // an oversized payload is dropped, not parsed
          done();
          process.stdin.destroy();
        }
      });
      process.stdin.on("end", done);
      process.stdin.on("error", done);
    } catch {
      resolve({});
    }
    setTimeout(done, 1000).unref();
  });
}
