// loom grep shim runtime: runs the native grep/rg and, when the output shape
// allows, names the function each hit sits in from loom's symbol table
// (code.enclosing). The compatibility promise is in ../README.md; the
// parsing and annotation rules are the pure functions in ./annotate.mjs.
//
// Invariants:
//   * stdout carries every byte the native tool printed, in order; an
//     annotated match line gains only a suffix before its newline
//   * stdin and stderr are the native tool's own (inherited)
//   * exit status is the native tool's; a native tool killed by a signal
//     makes the shim die by the same signal
//   * any failure, timeout or missing configuration means native output
//   * a terminal on stdout, or LOOM_GREP_SHIM=off, means plain pass-through

import { execFileSync, spawn } from "node:child_process";
import { accessSync, closeSync, constants, openSync, readFileSync, readSync, realpathSync, statSync } from "node:fs";
import { constants as osConstants, homedir } from "node:os";
import { isAbsolute, join, relative, resolve } from "node:path";
import {
  annotatable,
  fileOperands,
  foldResults,
  hitSuffixes,
  locationBatches,
  parseHits,
  renderGraphBlock,
} from "./annotate.mjs";

/** Every shim file carries this string in its first bytes; a PATH entry
 *  that does is another copy of the shim, never the native tool. */
export const SHIM_MARKER = "loom grep shim";

const FORWARDED_SIGNALS = ["SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"];

function settings(env) {
  return {
    timeoutMs: Number(env.LOOM_GREP_SHIM_TIMEOUT_MS || 2500),
    bufferCap: Number(env.LOOM_GREP_SHIM_MAX_BYTES || 8 * 1024 * 1024),
    quiet: !!env.LOOM_GREP_SHIM_QUIET,
    graph: (env.LOOM_GREP_SHIM_GRAPH || "off").toLowerCase() === "on",
    graphK: Math.max(1, Math.min(20, Number(env.LOOM_GREP_SHIM_GRAPH_K) || 5)),
    graphMaxAnchors: Number(env.LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS) || 20,
  };
}

function isShim(file) {
  let fd;
  try {
    fd = openSync(file, "r");
    const buf = Buffer.alloc(512);
    const n = readSync(fd, buf, 0, buf.length, 0);
    return buf.subarray(0, n).toString("latin1").includes(SHIM_MARKER);
  } catch {
    return false;
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

/** The native tool: LOOM_GREP_REAL / LOOM_RG_REAL when set, else the first
 *  executable of that name on PATH that is not a copy of the shim. */
export function nativeTool(tool, env = process.env, self = process.argv[1]) {
  const forced = env[tool === "rg" ? "LOOM_RG_REAL" : "LOOM_GREP_REAL"];
  if (forced) return forced;
  let selfReal = "";
  try {
    selfReal = realpathSync(self);
  } catch {
    /* unknown self: the marker still guards */
  }
  for (const dir of (env.PATH || "").split(":")) {
    if (!dir) continue;
    const cand = join(dir, tool);
    try {
      accessSync(cand, constants.X_OK);
      const real = realpathSync(cand);
      if (real === selfReal || isShim(real)) continue;
      return cand;
    } catch {
      /* next */
    }
  }
  return tool === "rg" ? "/usr/bin/rg" : "/usr/bin/grep";
}

/** Leave the way the native tool left: its status, or its signal. */
function exitLike(code, signal) {
  if (signal) {
    process.removeAllListeners(signal);
    try {
      process.kill(process.pid, signal);
    } catch {
      /* fall through to the conventional status */
    }
    // Node ignores SIGPIPE, so a native tool that died of it ends as a shell reports it.
    process.exit(128 + (osConstants.signals[signal] || 0));
  }
  process.exit(code === null ? 1 : code);
}

/** Run the native tool; forward the shim's signals to it. */
function runNative(bin, argv, tool, stdout) {
  const child = spawn(bin, argv, { stdio: ["inherit", stdout, "inherit"], argv0: tool });
  for (const sig of FORWARDED_SIGNALS) process.on(sig, () => child.kill(sig));
  child.on("error", (e) => {
    process.stderr.write(`${tool}: ${bin}: ${e.code === "ENOENT" ? "not found" : e.message}\n`);
    process.exit(e.code === "ENOENT" ? 127 : 126);
  });
  return child;
}

function passThrough(bin, argv, tool) {
  runNative(bin, argv, tool, "inherit").on("close", exitLike);
}

function loomConfig(env) {
  try {
    const p = env.LOOM_CONFIG_FILE || join(homedir(), ".loom", "config.json");
    const parsed = JSON.parse(readFileSync(p, "utf8"));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function resolveToken(env, cfg) {
  const fromEnv = (env.LOOM_API_KEY || "").trim();
  if (fromEnv) return fromEnv;
  const c = String(cfg.token || "").trim();
  if (c) return c;
  try {
    return readFileSync(env.LOOM_API_KEY_FILE || join(homedir(), ".loom", "token"), "utf8").trim();
  } catch {
    return "";
  }
}

function repoRoot() {
  try {
    return (
      execFileSync("git", ["rev-parse", "--show-toplevel"], {
        encoding: "utf8",
        timeout: 1000,
        stdio: ["ignore", "pipe", "ignore"],
      }).trim() || process.cwd()
    );
  } catch {
    return process.cwd();
  }
}

/** The single FILE operand, when there is exactly one: grep then prints
 *  `line:text` without the filename and the path comes from the operand. */
function singleFileOperand(argv, tool) {
  const ops = fileOperands(argv, tool);
  if (ops.length !== 1 || ops[0] === "-") return null;
  try {
    return statSync(ops[0]).isFile() ? ops[0] : null;
  } catch {
    return null;
  }
}

/** Write to stdout; a reader that went away ends the shim quietly. */
function onStdoutClosed(child) {
  process.stdout.on("error", (e) => {
    if (e.code !== "EPIPE") return;
    child.kill("SIGPIPE");
    exitLike(null, "SIGPIPE");
  });
}

/** Entry point for bin/grep and bin/rg. */
export function main(tool, argv = process.argv.slice(2), env = process.env) {
  const bin = nativeTool(tool, env);
  const single = singleFileOperand(argv, tool);
  if (env.LOOM_GREP_SHIM === "off" || process.stdout.isTTY || !annotatable(argv, tool, { singleFile: !!single })) {
    return passThrough(bin, argv, tool);
  }
  const cfg = loomConfig(env);
  const token = resolveToken(env, cfg);
  const url = String(env.LOOM_BASE_URL || cfg.url || "").replace(/\/+$/, "");
  if (!token || !url) return passThrough(bin, argv, tool);
  const opts = settings(env);

  // Buffer stdout up to the cap; past it, stream (an agent grepping the whole
  // tree for "a" must not wait on the index).
  const child = runNative(bin, argv, tool, "pipe");
  onStdoutClosed(child);
  const chunks = [];
  let size = 0;
  let streaming = false;
  const onData = (d) => {
    chunks.push(d);
    size += d.length;
    if (size <= opts.bufferCap) return;
    streaming = true;
    child.stdout.off("data", onData);
    process.stdout.write(Buffer.concat(chunks));
    chunks.length = 0;
    child.stdout.pipe(process.stdout, { end: false });
  };
  child.stdout.on("data", onData);
  child.on("close", async (code, signal) => {
    const finish = () => exitLike(code, signal);
    if (streaming) return void process.stdout.write("", finish);
    const raw = Buffer.concat(chunks);
    let out = raw;
    if (raw.length && !signal && code <= 1) {
      try {
        out = await decorate(raw, { tool, single, env, cfg, token, url, opts });
      } catch {
        out = raw;
      }
    }
    process.stdout.write(out, finish);
  });
}

/** The annotated output as bytes: grep's lines, each unchanged except for a
 *  suffix on a resolved hit, then the optional graph block. */
async function decorate(raw, { single, env, cfg, token, url, opts }) {
  const trailingNl = raw[raw.length - 1] === 0x0a;
  const body = trailingNl ? raw.subarray(0, raw.length - 1) : raw;
  const lineBufs = [];
  for (let start = 0; start <= body.length; ) {
    const nl = body.indexOf(0x0a, start);
    const end = nl < 0 ? body.length : nl;
    lineBufs.push(body.subarray(start, end));
    start = end + 1;
    if (nl < 0) break;
  }
  // Decoded only to find `path:line:` prefixes; the bytes written are the originals.
  const lines = lineBufs.map((b) => b.toString("utf8"));
  const root = repoRoot();
  const cwd = process.cwd();
  const relCache = new Map();
  const toRel = (p) => {
    if (relCache.has(p)) return relCache.get(p);
    let rel = null;
    try {
      const abs = isAbsolute(p) ? p : resolve(cwd, p);
      const r = relative(root, abs);
      if (r && !r.startsWith("..") && !isAbsolute(r) && statSync(abs).isFile()) rel = r;
    } catch {
      rel = null;
    }
    relCache.set(p, rel);
    return rel;
  };
  // -H forces `path:line:` even for one operand; detect by output shape.
  const withPath = single && lines.some((l) => /^\d+[:-]/.test(l)) ? single : null;
  const hits = parseHits(lines, toRel, withPath);
  if (!hits.length) return raw;
  const batches = locationBatches(hits);
  const started = Date.now();
  const ns = String(env.LOOM_CODE_NAMESPACE || cfg.code_namespace || "");
  const agent = String(env.LOOM_AGENT || cfg.agent || "grep-shim");
  const scopes = new Map();
  let asOf = "";
  let neighbours = [];
  let anchorsCapped = false;
  const signal = AbortSignal.timeout(opts.timeoutMs);
  const responses = await Promise.all(
    batches.map(async (locations, i) => {
      // The graph block comes from the FIRST batch only: the server caps it
      // at 20 functions, and the first 200 distinct hits are where they are.
      const graph = opts.graph && i === 0 ? { neighbours: true, neighbour_k: opts.graphK } : {};
      const res = await fetch(`${url}/v1/code.enclosing`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
        body: JSON.stringify({ locations, namespace: ns, agent, consumer: "grep-shim", ...graph }),
        signal,
      });
      if (!res.ok) throw new Error(`code.enclosing ${res.status}`);
      return res.json();
    }),
  );
  for (const r of responses) {
    foldResults(scopes, r.results);
    asOf = asOf || String((r.as_of && r.as_of.commit) || "").slice(0, 10);
    if (Array.isArray(r.neighbours) && r.neighbours.length) {
      neighbours = r.neighbours;
      anchorsCapped = !!r.anchors_capped;
    }
  }
  const suffixes = hitSuffixes(hits, scopes);
  const parts = [];
  lineBufs.forEach((b, idx) => {
    if (idx) parts.push(Buffer.from("\n"));
    parts.push(b);
    if (suffixes.has(idx)) parts.push(Buffer.from(suffixes.get(idx)));
  });
  if (trailingNl) parts.push(Buffer.from("\n"));
  // Strictly trailing, and only after newline-terminated output, so that
  // stripping the block gives grep's bytes back exactly.
  const block =
    opts.graph && trailingNl
      ? renderGraphBlock(neighbours, { maxAnchors: opts.graphMaxAnchors, capped: anchorsCapped })
      : "";
  if (block) parts.push(Buffer.from(block));
  if (!opts.quiet) {
    const took = Date.now() - started;
    const sent = batches.flat().length;
    const more = hits.length > sent ? ` (first ${sent} distinct)` : "";
    const related = block ? `, ${block.split("\n").length - 2} function(s) related from the graph` : "";
    process.stderr.write(
      `loom: ${suffixes.size}/${hits.length} hits scoped to their enclosing function via code.enclosing` +
        `${asOf ? `, index at ${asOf}` : ""}, ${took} ms${more}${related}\n`,
    );
  }
  return Buffer.concat(parts);
}
