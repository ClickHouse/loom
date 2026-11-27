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
// shell command, and `writtenSince` drops the ones last written before the
// session began, so a tree left dirty before the session (a branch parked
// mid-change, a teammate's edits) does not gate a turn that changed nothing,
// even one that read or ran those files.

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
  const runs = onePytest(raw.slice(0, maxRuns)).map((r) => withPython(r, python));
  const unknown = tests
    .filter((t) => !t.run)
    .map((t) => String(t.test_path || ""))
    .filter(Boolean);
  const untested = Array.isArray(gate.untested) ? gate.untested : [];
  const parts = [];
  if (runs.length) {
    const more = raw.length - maxRuns;
    parts.push(`run: ${runs.join(" ; ")}${more > 0 ? ` (+${more} more)` : ""}`);
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
      ? "Run the tests before finishing, or say why not."
      : "Add a test that reaches it, or say why none is needed.";
  const env =
    python === "" && raw.some(isPytest)
      ? " No Python was found for this checkout (no .venv, venv or VIRTUAL_ENV, no python3 or python on PATH): name the test you would run instead of searching for one."
      : "";
  return `Loom test gate${floor}: ${parts.join(" · ")}. ${tail}${env}`;
}

const isPytest = (run) => /^pytest(\s|$)/.test(String(run || ""));

/** `runs` with every `pytest <path>` folded into the first one: one command to
 *  copy and one process to start. A run with an option or a second word stays
 *  its own command. */
function onePytest(runs) {
  const plain = runs.filter((r) => /^pytest [^\s-]\S*$/.test(r));
  if (plain.length < 2) return runs;
  const merged = `pytest ${plain.map((r) => r.slice(7)).join(" ")}`;
  return runs.flatMap((r) => (r === plain[0] ? [merged] : plain.includes(r) ? [] : [r]));
}

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
  const roots = root ? [root] : [];
  const logical = root ? logicalRoot(root, cwd) : "";
  if (logical && logical !== root) roots.push(logical);
  return dirty.filter((rel) => {
    if (edited.has(rel)) return true;
    const absPaths = roots.flatMap((r) => absoluteForms(path.join(r, rel)));
    return commands.some((c) => commandNames(c, rel, absPaths));
  });
}

/** The time of the session's first transcript row, in epoch ms; 0 when no
 *  row carries one. */
export function sessionStart(objs) {
  for (const o of objs || []) {
    const at = Date.parse((o && o.timestamp) || "");
    if (Number.isFinite(at)) return at;
  }
  return 0;
}

/** `paths` without the files last written before `since` (epoch ms): a file
 *  left dirty before the session that a command of this session only read or
 *  ran is not this session's change. A file that cannot be read (a deleted
 *  one) stays, and so does every path when `since` is unknown. */
export function writtenSince(root, paths, since) {
  if (!since) return paths;
  return paths.filter((rel) => {
    try {
      return fs.statSync(path.join(root, rel)).mtimeMs >= since;
    } catch {
      return true;
    }
  });
}

// The prompt's last sentence, whole: "Do not run the test suite.", "Please
// don't run any tests". A sentence that says more (`do not run
// tests/test_slow.py`, "If lint fails, do not run the tests.") or that is not
// the last does not rule the run out.
const DECLINES =
  /(?:^|[.!?]\s+)(?:please\s+)?(?:do\s+not|don['’]?t|never)\s+run\s+(?:(?:the|any)\s+)?(?:tests|test\s+suite)[.!]*$/i;

/** True when the person's latest prompt ends by telling the agent not to run
 *  tests. Only the latest: "now run them" in a later prompt takes the earlier
 *  one back. Tool results and rows the client injected (isMeta: hook
 *  feedback, a skill body) are not the person speaking. */
export function declinesTests(objs) {
  let prompt = "";
  for (const o of objs || []) {
    if (!o || o.type !== "user" || o.isSidechain || o.isMeta || o.isCompactSummary) continue;
    const content = o.message && o.message.content;
    if (Array.isArray(content) && content.some((b) => b && b.type === "tool_result")) continue;
    prompt = contentText(content).trim();
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
// "passed" and names nothing deselected, so a -k subset is not one. --lf
// leaves tests out at collection and prints the line of a full run.
const SUMMARY = /\b\d+ (?:passed|failed|errors?)\b.* in [\d.]+s\b/;
const CLEAN = /^=*\s*\d+ passed(?:, \d+ (?:skipped|xfailed|xpassed|warnings?))* in [\d.]+s(?: \([\d:]+\))?\s*=*$/;
// The directory a command moves to before it runs something, and any move.
const CD = /(?:^|[;&|(\n])\s*cd\s+([^\s;&|)]+)/g;
const MOVE = /\b(?:cd|pushd|popd)\s/g;
const unquoted = (word) => word.replace(/^["']|["']$/g, "");
// pytest flags known to take no value. The word after any other flag written
// without `=` may be its value (`--cov src`, `--rootdir sub`), not a path
// that ran.
const BARE = /^-[qvxsl]+$/;

/** True when `text` closes a parenthesis it did not open. */
function closesParen(text) {
  let depth = 0;
  for (const ch of text) {
    if (ch === "(") depth += 1;
    else if (ch === ")" && --depth < 0) return true;
  }
  return false;
}
// What a command may hold from its first pytest on: pytest runs, and tail,
// head, grep and echo, with no backquote and no redirect but one between
// descriptors or to /dev/null. Anything else may change a file, and the
// command's result row is later than that change. Quotes are not read, so
// a `|` or `;` inside one ends the simple command there.
const OUTPUT = /^\s*(?:(?:tail|head|grep|echo)(?=\s|$)|$)/;
const onlyRuns = (tail) =>
  tail
    .replace(/\d?>(?:&\d|\s*\/dev\/null(?=[\s;&|)]|$))/g, " ")
    .split(/[;&|()\n]/)
    .every((part) => !/[<>`]/.test(part) && (OUTPUT.test(part) || part.match(PYTEST)));

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
 * skipped). A run counts when its Bash command finished no earlier than
 * `since`, the newest time of an uncommitted or untracked file, so an edit
 * by a subagent or a script the transcript never shows forgets it. An edit
 * tool or any other shell command after it forgets every earlier run: it
 * may have deleted a file or put one back as HEAD has it, which leaves no
 * newer time. A command may change files and then test them
 * (`cat >> tests/test_a.py <<EOF … EOF; pytest tests`), but not after its
 * first pytest (`onlyRuns`), and one that holds anything before that pytest
 * but its launcher forgets every earlier run. A run's paths are read from
 * the directory it ran in: the row's own cwd, moved by at most one `cd`
 * before the first run.
 */
function cleanRuns(objs, { root, cwd, since } = {}) {
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
      if (EDIT_TOOLS.has(name)) clean = [];
      if (name !== "Bash" && name !== "PowerShell") continue;
      const command = commandText(b.input);
      // Bash only: this reader follows no PowerShell move (`Set-Location`).
      const runs = name === "Bash" ? [...command.matchAll(PYTEST)] : [];
      if (!runs.length) {
        clean = [];
        continue;
      }
      const before = command.slice(0, runs[0].index);
      const cd = [...before.matchAll(CD)];
      const result = results.get(b.id) || {};
      // --ignore, --deselect, --lf and --sw leave out tests a clean summary
      // does not count; a quoted argument may be a flag's value in several
      // words; a move the reader cannot follow (a second `cd`, one between
      // two runs, one in a subshell that closed before the run, `pushd`)
      // leaves the paths unknown.
      const ran =
        allPassed(result.output, runs.length) &&
        result.at >= since &&
        runs.every((run) => !/["']/.test(run[1]) && ranHere(command.slice(0, run.index))) &&
        cd.length < 2 &&
        (command.match(MOVE) || []).length === cd.length &&
        cd.every((m) => !closesParen(before.slice(m.index + m[0].length))) &&
        !/--(?:ignore|deselect|lf|last-failed|sw|stepwise)\b/.test(command) &&
        onlyRuns(command.slice(runs[0].index));
      if (!ran) {
        clean = [];
        continue;
      }
      // What the command did before its first pytest may have changed the
      // tree the earlier runs tested, with no newer file time to show it.
      if (!LAUNCHER.test(before)) clean = [];
      const dir = o.cwd || cwd;
      const base = cd.length ? path.resolve(dir || root || "", unquoted(cd[0][1])) : dir;
      for (const run of runs) {
        let value = false;
        for (const word of run[1].split(/\s+/)) {
          const flag = word.startsWith("-");
          const rel = flag || value ? "" : relativeToRoot(root, word, base);
          if (rel) clean.push(rel);
          value = flag && !word.includes("=") && !BARE.test(word);
        }
      }
    }
  }
  return clean;
}

/**
 * True when the gate's ask is to run tests this checkout has and the
 * transcript already answers it: the person's latest prompt ends by saying
 * not to run tests, or `cleanRuns` holds every named test (its file, or a
 * directory above it); `lastModified()` is the tree's newest file time in
 * epoch ms. The stop then names no untested function either. A gate that
 * names a test missing from the checkout, or only untested functions, still
 * asks; so does one whose tests another runner runs, unless the person
 * ruled the run out. A Codex rollout answers nothing, so its stop blocks.
 */
export function runAnswered(gate, objs, { root, cwd, testExists, lastModified } = {}) {
  const tests = gate && Array.isArray(gate.tests) ? gate.tests : [];
  const paths = [...new Set(tests.map((t) => String(t.test_path || "")))];
  if (!paths.length || !paths.every((p) => p && testExists(p))) return false;
  if (declinesTests(objs)) return true;
  if (!tests.every((t) => isPytest(t.run))) return false;
  const clean = cleanRuns(objs, { root, cwd, since: lastModified() });
  return paths.every((p) => clean.some((c) => p === c || p.startsWith(`${c}/`)));
}

/** The newest modification time, in epoch ms, of `files` under `root`.
 *  Infinity when the list is unknown (null) or a file cannot be read (a
 *  deleted tracked file), so that no test run counts as later. */
export function newestChange(root, files) {
  if (!files) return Infinity;
  let last = 0;
  try {
    for (const file of files) last = Math.max(last, fs.statSync(path.join(root, file)).mtimeMs);
  } catch {
    return Infinity;
  }
  return last;
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
