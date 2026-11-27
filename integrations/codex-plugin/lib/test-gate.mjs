// Helpers for the loom-test-gate Stop hook (unit-tested).
//
// When the agent is about to stop with uncommitted changes in a loom-indexed
// repo, ask `code.test_gate` what the change obliges: the tests that reach
// it (evidence-ordered, with a run command when derivable) and the touched
// functions no test reaches. If either list is non-empty and this exact
// change has not been gated yet this session, block the stop ONCE with a
// reason that names them: the tests are named before "done", not discovered
// in review. The second stop on the
// same change passes: the agent has seen the obligation and either ran the
// tests or chose not to, and a gate that nags is a gate that gets disabled.
// A stop whose transcript already answers the ask is not blocked at all
// (`runAnswered`): a block costs the session one more model request.
//
// The change is the session's, not the tree's: `sessionScope` keeps only
// the dirty paths the transcript shows this session edited or named in a
// shell command, so a tree left dirty before the session (a branch parked
// mid-change, a teammate's edits) does not gate a turn that touched nothing.

import fs from "node:fs";
import path from "node:path";
import { commandText, contentText, extractFiles, normalizeToolName, pathUnder } from "./loom-client.mjs";

const EDIT_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit", "str_replace_editor", "apply_patch"]);

/** Stable signature of the change: sorted changed paths + the diff's hash. */
export function gateSignature(changedPaths, diffHash) {
  const paths = (changedPaths || []).map(String).filter(Boolean).sort();
  return `${paths.join(",")}|${String(diffHash || "")}`;
}

/** Block only the first time this signature is seen in the session. */
export function decideBlock(cache, signature) {
  const seen = { ...((cache && cache.seen) || {}) };
  if (seen[signature]) return { block: false, cache: { seen } };
  seen[signature] = Date.now();
  const keys = Object.keys(seen);
  if (keys.length > 100) keys.slice(0, keys.length - 100).forEach((k) => delete seen[k]);
  return { block: true, cache: { seen } };
}

function short(qname) {
  const parts = String(qname || "").split("::");
  return parts.length > 2 ? parts.slice(-2).join("::") : String(qname || "");
}

/**
 * Render the obligation text, or "" when there is nothing to say.
 * Runnable commands first (deduped), then tests with no derivable command,
 * then the untested functions. Bounded: maxRuns commands, maxNames names.
 */
export function renderGate(gate, { maxRuns = 4, maxNames = 4, python, testExists } = {}) {
  if (!gate || !gate.obligations) return "";
  const indexed = Array.isArray(gate.tests) ? gate.tests : [];
  // A test map may retain a deleted path, or the checkout may differ from
  // the indexed revision. Never prescribe a command whose target is absent.
  // Keep that evidence visible: an unavailable test is not a green test.
  const missing = testExists
    ? [...new Set(indexed.map((t) => String(t.test_path || "")).filter((p) => p && !testExists(p)))]
    : [];
  const absent = new Set(missing);
  const tests = indexed.filter((t) => !absent.has(String(t.test_path || "")));
  const raw = [...new Set(tests.map((t) => String(t.run || "")).filter(Boolean))];
  const runs = raw.map((r) => withPython(r, python));
  const unknown = tests
    .filter((t) => !t.run)
    .map((t) => String(t.test_path || ""))
    .filter(Boolean);
  const untested = Array.isArray(gate.untested) ? gate.untested : [];
  const parts = [];
  if (runs.length) {
    const shown = runs.slice(0, maxRuns);
    const more = runs.length - shown.length;
    parts.push(`run: ${shown.join(" ; ")}${more > 0 ? ` (+${more} more)` : ""}`);
  }
  if (unknown.length) {
    const shown = unknown.slice(0, maxNames);
    const more = unknown.length - shown.length;
    parts.push(`also reached (no known runner): ${shown.join(", ")}${more > 0 ? ` (+${more})` : ""}`);
  }
  if (missing.length) {
    const shown = missing.slice(0, maxNames);
    const more = missing.length - shown.length;
    parts.push(`indexed tests not present in this checkout: ${shown.join(", ")}${more > 0 ? ` (+${more})` : ""}`);
  }
  if (untested.length) {
    const names = [...new Set(untested.map((u) => short(u.qualified_name)))].filter(Boolean);
    const shown = names.slice(0, maxNames);
    const more = (gate.untested_total || names.length) - shown.length;
    parts.push(`no test reaches: ${shown.join(", ")}${more > 0 ? ` (+${more})` : ""}`);
  }
  if (!parts.length) return "";
  const floor = gate.counts_floor ? " (graph is a floor)" : "";
  // Nothing to run is not "run the tests" — a session told
  // that went hunting for a runner. Say what the obligation actually is.
  const tail = missing.length
    ? "Run the available tests and check the missing paths against the indexed revision (deleted, renamed or different checkout); use current equivalent tests or say why not. Missing tests are not passes."
    : runs.length || unknown.length
      ? "Run the tests and rely on green, or say why not, before finishing."
      : "Add a test that reaches it, or say why the change needs none, before finishing.";
  const env =
    python === "" && raw.some(isPytest)
      ? " No Python was found for this checkout (no .venv, venv or VIRTUAL_ENV, no python3 or python on PATH): name the test you would run instead of searching for one."
      : "";
  return `Loom test gate${floor}: ${parts.join(" · ")}. ${tail}${env}`;
}

const isPytest = (run) => /^pytest(\s|$)/.test(String(run || ""));

/** `pytest …` as this checkout runs it: `<python> -m pytest …` when a
 *  checkout interpreter was found; unchanged when `python` is unknown
 *  (undefined) or none was found (""). */
export function withPython(run, python) {
  return python && isPytest(run) ? `${python} -m ${run}` : run;
}

// Line-comment markers per extension. A diff whose every changed line is
// blank or one of these changes no behaviour, so it owes no test. A bare
// `*` is not one: `*ptr = 1;` is C, so a block comment's middle lines gate.
const COMMENT_MARKERS = [
  [/\.(py|pyi|sh|bash|rb|pl|r|ya?ml|toml|cfg|ini|conf|mk)$|(^|\/)(Makefile|Dockerfile)$/i, ["#"]],
  [/\.(m?[jt]sx?|cjs|cts|mts|go|rs|java|kt|kts|scala|swift|c|cc|cpp|cxx|h|hh|hpp|cs|php|dart|proto)$/i, ["//", "/*"]],
  [/\.sql$/i, ["--"]],
];

function markersFor(file) {
  for (const [re, marks] of COMMENT_MARKERS) if (re.test(file)) return marks;
  return null;
}

/**
 * True when every added or removed line of a `git diff --unified=0` is
 * blank or a line comment of its file's language: a trailing `# note` in a
 * .py file changes no behaviour and owes no test. A file of an unknown
 * language, a rename or a mode change counts as code; so does an empty diff.
 */
export function commentOnlyDiff(diff) {
  let file = "";
  let marks = null;
  let changedLines = 0;
  for (const line of String(diff || "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      file = "";
      marks = null;
      continue;
    }
    if (line.startsWith("+++ ")) {
      file = line.slice(4).replace(/^b\//, "");
      marks = file === "/dev/null" ? null : markersFor(file);
      continue;
    }
    if (line.startsWith("--- ")) continue;
    if (/^(rename|similarity|new file|deleted file|old mode|new mode|Binary files)/.test(line)) return false;
    if (!(line.startsWith("+") || line.startsWith("-"))) continue;
    changedLines += 1;
    const body = line.slice(1).trim();
    if (!body) continue;
    if (!marks || !marks.some((m) => body.startsWith(m))) return false;
  }
  return changedLines > 0;
}

/** The interpreter of the virtualenv at `dir`: bin/python, or on Windows
 *  Scripts\python.exe. */
export function venvPython(dir, platform = process.platform) {
  return platform === "win32" ? path.win32.join(dir, "Scripts", "python.exe") : path.posix.join(dir, "bin", "python");
}

/** The interpreter that runs this checkout's pytest: its own virtualenv
 *  first, then an active one, then a python3 or python on PATH; "" when there
 *  is none. Nothing is run to check for pytest: a Python started to ask would
 *  run what the checkout puts on its path at startup. */
export function checkoutPython(root, { env = process.env } = {}) {
  const win = process.platform === "win32";
  const cands = [
    venvPython(path.join(root, ".venv")),
    venvPython(path.join(root, "venv")),
    env.VIRTUAL_ENV ? venvPython(env.VIRTUAL_ENV) : "",
  ].filter(Boolean);
  for (const c of cands) {
    if (isFile(c)) return pathUnder(root, c) ?? c;
  }
  const dirs = (env.PATH ?? "")
    .split(path.delimiter)
    .map((d) => (win ? d.replace(/^"(.*)"$/, "$1") : d))
    .filter(Boolean);
  // Windows: python first, since python.org installs ship no python3.exe;
  // .bat and .cmd are shims such as pyenv-win's, which the shell runs too.
  const exts = win ? [".exe", ".bat", ".cmd"] : [""];
  for (const py of win ? ["python", "python3"] : ["python3", "python"]) {
    if (dirs.some((d) => exts.some((e) => isProgram(path.join(d, py + e), win)))) return py;
  }
  return "";
}

function isFile(p) {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

/** A PATH entry a shell would start: executable on POSIX, and not empty, so
 *  the zero-byte Store aliases Windows puts on PATH when no Python is
 *  installed do not count. */
function isProgram(p, win) {
  try {
    const st = fs.statSync(p);
    if (!st.isFile() || st.size === 0) return false;
    if (!win) fs.accessSync(p, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/** Stop-hook payload: block once with the reason; empty text = no output. */
export function gatePayload(text) {
  return text ? { decision: "block", reason: text } : null;
}

/** `p` as a repo-relative posix path under `root` (resolved against `cwd`
 *  when relative); "" when it lies outside the repo or `root` is unknown. */
function relativeToRoot(root, p, cwd) {
  const s = String(p || "").trim();
  if (!s || !root) return "";
  const abs = path.isAbsolute(s) ? s : path.resolve(cwd || root, s);
  return pathUnder(root, abs) || "";
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** True when `command` names `rel` (or its absolute form) as a whole path:
 *  `sed -i s/a/b/ src/a.py`, `pytest tests/test_a.py::test_x`, a heredoc
 *  into it. `data.py` does not name `a.py`; `a.pyc` does not name `a.py`. */
function commandNames(command, rel, absPaths) {
  const alts = [rel, ...absPaths].map(escapeRe);
  return new RegExp(`(^|[^A-Za-z0-9_./-])(?:${alts.join("|")})(?![A-Za-z0-9_-])`).test(command);
}

/**
 * The subset of `changed` (repo-relative paths, `git diff --name-only`
 * order) this session touched, read from the transcript's assistant
 * tool_use blocks, or Codex rollout response_item tool calls: the target of
 * an edit tool (Edit, Write, MultiEdit, NotebookEdit, apply_patch), or a path
 * a shell command named — the command text is the evidence for a `sed -i`,
 * a heredoc, a test run.
 * Sidechains are skipped. Edits the transcript cannot see (a script that
 * writes files) are missed: the list is a floor, like the graph counts the
 * gate reports. `root` is the repo top level; `cwd` resolves relative paths.
 */
export function sessionScope(changed, objs, { root, cwd } = {}) {
  const dirty = (changed || []).map(String).filter(Boolean);
  if (!dirty.length) return [];
  const edited = new Set();
  const commands = [];
  for (const o of objs || []) {
    if (o?.type === "response_item") {
      const call = o.payload || {};
      if (
        ["custom_tool_call", "function_call"].includes(call.type) &&
        ["exec", "exec_command", "shell", "apply_patch"].includes(call.name)
      ) {
        const input = call.input ?? call.arguments;
        if (typeof input === "string") commands.push(input);
      }
      continue;
    }
    if (!o || o.type !== "assistant" || o.isSidechain) continue;
    const content = o.message && o.message.content;
    if (!Array.isArray(content)) continue;
    for (const b of content) {
      if (!b || b.type !== "tool_use") continue;
      const name = normalizeToolName(b.name);
      if (EDIT_TOOLS.has(name)) {
        for (const p of extractFiles(b.input)) {
          const rel = relativeToRoot(root, p, cwd);
          if (rel) edited.add(rel);
        }
      } else if (name === "Bash" || name === "PowerShell") {
        // A path a command names reads the same in either shell.
        const text = commandText(b.input);
        if (text) commands.push(text);
      }
    }
  }
  const names = pathNamer(root, cwd);
  return dirty.filter((rel) => edited.has(rel) || commands.some((c) => names(c, rel)));
}

/** `(command, rel)`: does the shell command name repo-relative `rel`, as
 *  written or under either spelling of the checkout root? */
function pathNamer(root, cwd) {
  const roots = root ? [root] : [];
  const logical = root ? logicalRoot(root, cwd) : "";
  if (logical && logical !== root) roots.push(logical);
  return (command, rel) =>
    commandNames(
      command,
      rel,
      roots.flatMap((r) => absoluteForms(path.join(r, rel))),
    );
}

// "Do not run the test suite", "don't run any tests yet": an instruction
// that opens its clause and ends it. `do not run tests/test_slow.py` and
// `don't run the tests that need Docker` rule out some tests, `agents that
// do not run tests` only mentions the phrase: neither rules out the run.
const DECLINES = new RegExp(
  "(?:^|[.!?;:,(\\n—–]\\s*|\\s-\\s+|\\b(?:please|and|but|so|then|also|just)\\s+)" +
    "(?:do\\s+not|don['’]?t|never)\\s+run\\s+(?:(?:the|any)\\s+)?(?:tests|test\\s+suite)" +
    "(?=(?:\\s+(?:yet|now|again|please|at\\s+all|for\\s+now|this\\s+time))?[ \\t]*" +
    "(?:$|\\n|[.!?;:,)—–]+(?:\\s|$)|\\s(?:-|and|or|but)\\s))",
  "i",
);

/** True when the person's latest prompt tells the agent not to run tests.
 *  Only the latest: "now run them" in a later prompt takes the earlier one
 *  back. Tool results and rows the client injected (isMeta: hook feedback,
 *  a skill body) are not the person speaking. */
export function declinesTests(objs) {
  let prompt = "";
  for (const o of objs || []) {
    if (!o || o.type !== "user" || o.isSidechain || o.isMeta || o.isCompactSummary) continue;
    const content = o.message && o.message.content;
    if (Array.isArray(content) && content.some((b) => b && b.type === "tool_result")) continue;
    prompt = contentText(content);
  }
  return DECLINES.test(prompt);
}

// A pytest invocation and its arguments up to the end of the simple command.
// `import pytest` in a heredoc is not one.
const PYTEST = /(?<=^|[\s/])(?<!import\s)py\.?test(?=\s|$)([^\n;|&)]*)/g;
// What may stand between the start of that simple command and pytest when it
// ran in this checkout: variable assignments, env, timeout, a runner, an
// interpreter. `docker exec app pytest` ran elsewhere; so did a script
// handed to one of ELSEWHERE (`ssh host 'cd repo; pytest tests'`).
const LAUNCHER = /^\s*(?:(?:env|-u\s+\w+|\w+=\S*|timeout\s+\S+|(?:uv|poetry)\s+run)\s+)*(?:\S+\s+-m\s+|\S*\/)?$/;
const ELSEWHERE = /\b(?:ssh|docker|podman|kubectl)\s/;
/** True when `head`, a command's text up to a pytest run, shows the run
 *  started in this checkout. */
const ranHere = (head) => !ELSEWHERE.test(head) && LAUNCHER.test(head.split(/[;&|(\n]/).pop());
// pytest's last line, whose order is fixed: failed, passed, skipped,
// deselected, xfailed, xpassed, warnings, errors. A clean line starts at
// "passed" and names nothing deselected, so a -k or --lf subset is not one.
const SUMMARY = /\b\d+ (?:passed|failed|errors?)\b.* in [\d.]+s\b/;
const CLEAN = /^=*\s*\d+ passed(?:, \d+ (?:skipped|xfailed|xpassed|warnings?))* in [\d.]+s(?: \([\d:]+\))?\s*=*$/;
// The directory a command moves to before it runs something, and any move.
const CD = /(?:^|[;&|(\n])\s*cd\s+([^\s;&|)]+)/g;
const MOVE = /\b(?:cd|pushd|popd)\s/g;
const unquoted = (word) => word.replace(/^["']|["']$/g, "");
// git subcommands that rewrite tracked files without naming them.
const TREE_GIT = /\bgit\s+(?:stash|checkout|restore|reset|switch|apply|merge|rebase|pull|revert|cherry-pick|am)\b/;

/** True when the output holds a clean pytest summary for each of `runs`
 *  invocations and no other summary: `| tail -5; echo exit=$?` hides the
 *  exit status, the summary line survives it. */
function allPassed(output, runs) {
  const lines = String(output || "")
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length < 300 && SUMMARY.test(l));
  return lines.length >= runs && lines.every((l) => CLEAN.test(l));
}

/**
 * The repo-relative files and directories pytest ran clean on the tree as
 * it stands, read in transcript order (Claude Code rows; sidechains
 * skipped). A run counts when its command finished no earlier than `since`,
 * the tree's last change, so an edit by a subagent or a script the
 * transcript never shows forgets it. A tree-rewriting git command, or a
 * pytest run that does not count, forgets every earlier run. A command may
 * change files and then test them (`cat >> tests/test_a.py <<EOF … EOF;
 * pytest tests`), so within one command only what follows its first pytest
 * is read for a change: a floor, since a rewrite there that names no changed
 * path is not seen. A run's paths are read from the directory it ran in:
 * the row's own cwd, moved by at most one `cd` before the first run.
 */
function cleanRuns(objs, { changed, root, cwd, since } = {}) {
  const names = pathNamer(root, cwd);
  const changes = (text) => TREE_GIT.test(text) || (changed || []).some((rel) => names(text, rel));
  const rows = (objs || []).filter((o) => o && !o.isSidechain && Array.isArray(o.message && o.message.content));
  const results = new Map();
  for (const o of rows) {
    if (o.type !== "user") continue;
    for (const b of o.message.content) {
      if (b && b.type === "tool_result") {
        results.set(b.tool_use_id, { output: contentText(b.content), at: Date.parse(o.timestamp) });
      }
    }
  }
  let clean = [];
  for (const o of rows) {
    if (o.type !== "assistant") continue;
    for (const b of o.message.content) {
      if (!b || b.type !== "tool_use") continue;
      const name = normalizeToolName(b.name);
      if (name !== "Bash" && name !== "PowerShell") continue;
      const command = commandText(b.input);
      const runs = [...command.matchAll(PYTEST)];
      if (!runs.length) {
        if (TREE_GIT.test(command)) clean = [];
        continue;
      }
      const before = command.slice(0, runs[0].index);
      const after = command.slice(runs[0].index).replace(PYTEST, " ");
      const cd = [...before.matchAll(CD)];
      const result = results.get(b.id) || {};
      // --ignore drops a path the summary never mentions; a move the reader
      // cannot follow (a second `cd`, one between two runs, `pushd`) leaves
      // the paths unknown.
      const ran =
        allPassed(result.output, runs.length) &&
        result.at >= since &&
        runs.every((run) => ranHere(command.slice(0, run.index))) &&
        cd.length < 2 &&
        (command.match(MOVE) || []).length === cd.length &&
        !command.includes("--ignore") &&
        !changes(after);
      if (!ran) {
        clean = [];
        continue;
      }
      const dir = o.cwd || cwd;
      const base = cd.length ? path.resolve(dir || root || "", unquoted(cd[0][1])) : dir;
      for (const run of runs) {
        for (const word of run[1].split(/\s+/)) {
          const rel = word.startsWith("-") ? "" : relativeToRoot(root, unquoted(word), base);
          if (rel) clean.push(rel);
        }
      }
    }
  }
  return clean;
}

/**
 * True when the gate's ask is to run tests this checkout has and the
 * transcript already answers it, so the gate has nothing to add: the
 * person's latest prompt said not to run tests, or pytest ran every named
 * test clean (its file, or a directory above it) and the tree has not
 * changed since: `lastModified()` is that change in epoch ms. The stop then
 * names no untested function either. A gate that names a test missing from
 * the checkout, or only untested functions, still asks; so does one whose
 * tests another runner runs, unless the person ruled the run out. Read from
 * Claude Code transcript rows: a Codex rollout answers nothing, so its stop
 * blocks.
 */
export function runAnswered(gate, objs, { changed, root, cwd, testExists, lastModified } = {}) {
  const tests = gate && Array.isArray(gate.tests) ? gate.tests : [];
  const paths = [...new Set(tests.map((t) => String(t.test_path || "")))];
  if (!paths.length || !paths.every((p) => p && testExists(p))) return false;
  if (declinesTests(objs)) return true;
  if (!tests.every((t) => isPytest(t.run))) return false;
  const clean = cleanRuns(objs, { changed, root, cwd, since: lastModified() });
  return paths.every((p) => clean.some((c) => p === c || p.startsWith(`${c}/`)));
}

/** The checkout root as `cwd` spells it, when that differs from git's: a
 *  session started in /Users/me/link/src, where link points at the
 *  checkout, names its files /Users/me/link/... in its commands. "" when
 *  `cwd` is outside `root`. */
function logicalRoot(root, cwd) {
  const rel = cwd ? pathUnder(root, cwd) : null;
  if (rel == null) return "";
  const abs = path.resolve(cwd);
  const tail = rel ? path.join(...rel.split("/")) : "";
  if (!tail) return abs;
  return abs.endsWith(path.sep + tail) ? abs.slice(0, -tail.length - 1) : "";
}

/** The spellings a shell command may use for absolute path `abs`: as given,
 *  and on Windows with forward slashes (C:/x), with either drive-letter case,
 *  and as Git Bash writes it (/c/x). */
function absoluteForms(abs) {
  const forms = new Set([abs, abs.split(path.sep).join("/")]);
  const drive = /^([A-Za-z]):[\\/]/.exec(abs);
  if (drive) {
    const rest = abs.slice(3);
    for (const d of [drive[1].toUpperCase(), drive[1].toLowerCase()]) {
      forms.add(`${d}:\\${rest.split("/").join("\\")}`);
      forms.add(`${d}:/${rest.split(path.sep).join("/")}`);
    }
    forms.add(`/${drive[1].toLowerCase()}/${rest.split(path.sep).join("/")}`);
  }
  return [...forms];
}
