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
//
// The change is the session's, not the tree's: `sessionScope` keeps only
// the dirty paths the transcript shows this session edited or named in a
// shell command, so a tree left dirty before the session (a branch parked
// mid-change, a teammate's edits) does not gate a turn that touched nothing.

import path from "node:path";
import { commandText, extractFiles, normalizeToolName, pathUnder } from "./loom-client.mjs";

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
      ? " No Python env with pytest was found in this checkout (no .venv, none on PATH): name the test you would run instead of searching for one."
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
      } else if (name === "Bash") {
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
