// Helpers for the loom-test-gate hook (unit-tested).
//
// The gate names the tests a change obliges, from `code.test_gate`. By
// default it names them when the file is edited: the tool call that changed
// a file carries one line with the command of the tests the index links to
// it, or of the file itself when it is a test, or with the changed functions
// the index links no test to when it has no test to name; once per file per
// session (`adviceFiles`, `adviceLines`).
//
// At Stop it holds a session that ran no test since its last code change.
// When the agent is about to stop with uncommitted changes in a
// loom-indexed repo, it asks
// `code.test_gate` what the change obliges: the tests that reach
// it (evidence-ordered, with a run command when derivable) and the touched
// functions no test reaches. If either list is non-empty and this exact
// change has not been gated yet this session, block the stop ONCE with a
// reason that names them: the tests are named before "done", not discovered
// in review. The second stop on the
// same change passes: the agent has seen the obligation and either ran the
// tests or chose not to, and a gate that nags is a gate that gets disabled.
// A block costs the session one more model request, so by default any test
// run since the last write to a changed code file lets the stop pass before
// anything is asked (`testedSince`, `lastCodeChange`): which tests it ran
// and how they went is the agent's to
// judge, and a hook that reads shell text to decide it blocks sessions that
// ran them. With LOOM_TEST_GATE=block nothing is named at edit time and the
// stop passes only when the transcript shows the named tests ran on the tree
// as it stands (`runAnswered`).
//
// The change is the session's, not the tree's: `sessionScope` keeps only
// the dirty paths the transcript shows this session edited or named in a
// shell command, and `writtenSince` drops the ones last written before the
// session began, so a tree left dirty before the session (a branch parked
// mid-change, a teammate's edits) does not gate a turn that changed nothing,
// even one that read or ran those files.

import fs from "node:fs";
import path from "node:path";
import {
  commandText,
  contentText,
  DIFF_ARGS,
  diffHeaders,
  envSwitch,
  extractFiles,
  git,
  gitToplevel,
  normalizeToolName,
  pathUnder,
  postBounded,
  resolveServerUrl,
} from "./loom-client.mjs";
import { readSessionState, writeSessionState } from "./tmp-state.mjs";
import { parseLines, readRange } from "./transcript.mjs";

const EDIT_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit", "str_replace_editor", "apply_patch"]);

/** What the gate does, from LOOM_TEST_GATE: "advise" names an edited file's
 *  tests on the tool call that edited it and holds the stop of a session that
 *  ran no test since its last code change (unset, or any on-word); "block" names
 *  nothing and holds the stop until the named tests ran; "off" does neither. */
export function gateMode(env = process.env) {
  if (
    String(env.LOOM_TEST_GATE ?? "")
      .trim()
      .toLowerCase() === "block"
  ) {
    return "block";
  }
  return envSwitch("LOOM_TEST_GATE", true, env) ? "advise" : "off";
}

/** The hops `code.test_gate` follows: LOOM_TEST_GATE_DEPTH, 1 to 3. */
export function gateDepth(env = process.env) {
  return Math.min(3, Math.max(1, Number(env.LOOM_TEST_GATE_DEPTH || 2)));
}

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
  const { raw, text: runs } = runList(tests, { maxRuns, python });
  const unknown = tests
    .filter((t) => !t.run)
    .map((t) => String(t.test_path || ""))
    .filter(Boolean);
  const untested = Array.isArray(gate.untested) ? gate.untested : [];
  const parts = [];
  if (runs) parts.push(`run: ${runs}`);
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
    const { list, total } = untestedNames(gate, maxNames);
    // Beside tests to run, "no test reaches" reads as a fact about them, and
    // a test that builds an object reaches a constructor the graph holds no
    // edge to.
    parts.push(
      runs || unknown.length
        ? `the index links no test to: ${list} (the tests named here may still reach ${total > 1 ? "them" : "it"})`
        : `no test reaches: ${list}`,
    );
  }
  if (!parts.length) return "";
  const floor = gate.counts_floor ? " (graph is a floor)" : "";
  // Nothing to run is not "run the tests" — a session told
  // that went hunting for a runner. Say what the obligation actually is.
  const tail = missing.length
    ? "Run the available tests and check the missing paths against the indexed revision (deleted, renamed or different checkout); use current equivalent tests or say why not. Missing tests are not passes."
    : runs || unknown.length
      ? "Run the tests before finishing, or say why not."
      : "Add a test that reaches it, or say why none is needed.";
  const env =
    python === "" && raw.some(isPytest)
      ? " No Python was found for this checkout (no .venv, venv or VIRTUAL_ENV, no python3 or python on PATH): name the test you would run instead of searching for one."
      : "";
  return `Loom test gate${floor}: ${parts.join(" · ")}. ${tail}${env}`;
}

const isPytest = (run) => /^pytest(\s|$)/.test(String(run || ""));

/** The functions of a gate's `untested`, by short name: the first `maxNames`
 *  as `list`, with a count of the rest, and how many there are in `total`. */
function untestedNames(gate, maxNames = 4) {
  const untested = gate && Array.isArray(gate.untested) ? gate.untested : [];
  const names = [...new Set(untested.map((u) => short(u.qualified_name)))].filter(Boolean);
  const shown = names.slice(0, maxNames);
  const total = (gate && gate.untested_total) || names.length;
  const more = total > shown.length ? ` (+${total - shown.length})` : "";
  return { list: shown.length ? `${shown.join(", ")}${more}` : "", total };
}

/** The distinct run commands of `tests` (`raw`) and the first `maxRuns` of
 *  them as one text, "" when no test has one. */
function runList(tests, { maxRuns, python }) {
  const raw = [...new Set(tests.map((t) => String(t.run || "")).filter(Boolean))];
  const runs = onePytest(raw.slice(0, maxRuns)).map((r) => withPython(r, python));
  const more = raw.length - maxRuns;
  return { raw, text: runs.length ? `${runs.join(" ; ")}${more > 0 ? ` (+${more} more)` : ""}` : "" };
}

/**
 * The tests of a `code.test_gate` answer about `rel` alone that the
 * edit-time line names: the ones the checkout has. For a source file they
 * are the tests the index links to it; for a test file, the file itself.
 * A file under a tests directory that no runner takes (a fixture, a
 * conftest) is not a test to run for its own change.
 */
export function adviceTests(rel, gate, testExists) {
  return (gate && Array.isArray(gate.tests) ? gate.tests : []).filter(
    (t) =>
      t && t.test_path && (t.run || String(t.test_path) !== rel) && (!testExists || testExists(String(t.test_path))),
  );
}

/**
 * The edit-time line for `rel` from `tests` (see `adviceTests`): their
 * commands, then the tests no runner is known for. With no test, it names
 * `untested`, the changed functions the index links no test to, as the
 * index's view: the graph is a floor, and the line has no reply in which the
 * agent could say that a test does reach them. "" when there is neither.
 */
export function renderAdvice(rel, tests, { maxRuns = 4, maxNames = 4, python, untested = "" } = {}) {
  const parts = [runList(tests, { maxRuns, python }).text].filter(Boolean);
  const unknown = [...new Set(tests.filter((t) => !t.run).map((t) => String(t.test_path)))];
  if (unknown.length) {
    const more = unknown.length - maxNames;
    parts.push(`no known runner: ${unknown.slice(0, maxNames).join(", ")}${more > 0 ? ` (+${more})` : ""}`);
  }
  if (!parts.length && untested) parts.push(`the index links no test to ${untested}`);
  return parts.length ? `Loom tests for ${rel}: ${parts.join(" · ")}` : "";
}

/** The tests of `tests` that `renderAdvice` prints: those of its first
 *  `maxRuns` commands and its first `maxNames` paths with no known runner. */
export function printedTests(tests, { maxRuns = 4, maxNames = 4 } = {}) {
  const runs = [...new Set(tests.map((t) => String(t.run || "")).filter(Boolean))].slice(0, maxRuns);
  const paths = [...new Set(tests.filter((t) => !t.run).map((t) => String(t.test_path)))].slice(0, maxNames);
  return tests.filter((t) => (t.run ? runs.includes(String(t.run)) : paths.includes(String(t.test_path))));
}

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

/** PostToolUse payload: additional context only, never a decision. */
export function advicePayload(text) {
  return text ? { hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: text } } : null;
}

/** A predicate: is `testPath` a file of the checkout at `root`? A stale
 *  index row or another checkout's test is not. */
export function testsIn(root) {
  return (testPath) => {
    const target = path.resolve(root, testPath);
    return Boolean(pathUnder(root, target)) && isFile(target);
  };
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
        (rolloutShell(call.name) || call.name === "apply_patch")
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

// How much of a transcript's head is read for the session's first row.
const HEAD_BYTES = 64 * 1024;
// Files asked about for one tool call: a command that names more waits for
// the next call that names them.
const MAX_ADVICE_FILES = 4;

/**
 * The files a PostToolUse payload's tool call changed that this session has
 * not been advised on, with the checkout root: the dirty paths its edit tool
 * targeted or its shell command named, written since the session began. That
 * is `sessionScope` and `writtenSince` over the one call, the rule the
 * blocking gate applies to the whole transcript, so a command that only reads
 * a file left dirty before the session names nothing. The hook runs after
 * every shell command, so a call that cannot name an unasked dirty file (a
 * path is named with its last segment) costs one git call, not two.
 */
export function adviceFiles(payload, { cwd, root = "" } = {}) {
  const dirty = git(["diff", "--name-only", "-z", "HEAD"], root || cwd, { timeoutMs: 2000, raw: true })
    .split("\0")
    .filter(Boolean);
  const seen = dirty.length ? readSessionState("testadvice", payload.session_id).seen || {} : {};
  const text = `${commandText(payload.tool_input)}\n${extractFiles(payload.tool_input).join("\n")}`;
  const unasked = dirty.filter((rel) => !seen[rel] && text.includes(rel.slice(rel.lastIndexOf("/") + 1)));
  const top = unasked.length ? root || gitToplevel(cwd) : "";
  if (!top) return { root, files: [] };
  const call = { type: "tool_use", name: payload.tool_name, input: payload.tool_input };
  const named = sessionScope(unasked, [{ type: "assistant", message: { content: [call] } }], { root: top, cwd });
  const head = named.length && payload.transcript_path ? readRange(payload.transcript_path, 0, HEAD_BYTES) : "";
  const files = writtenSince(top, named, sessionStart(parseLines(head.toString()))).slice(0, MAX_ADVICE_FILES);
  return { root: top, files };
}

/** `map` without its oldest keys beyond `max`. */
function newest(map, max = 300) {
  const keys = Object.keys(map);
  for (const k of keys.slice(0, Math.max(0, keys.length - max))) delete map[k];
  return map;
}

/** Adds `keys` to the session's test-advice map `field` (`seen`: files asked
 *  about; `retried`: files to ask about once more; `named`: tests a line
 *  named). The state is read and written here
 *  with nothing between, not locked: two hooks of one session that write in
 *  the same instant (parallel tool calls) keep one's entries, and the other's
 *  file is then asked about twice or its test named twice. */
function remember(sessionId, field, keys) {
  const state = readSessionState("testadvice", sessionId);
  const map = { ...state[field] };
  for (const key of keys) map[key] = Date.now();
  writeSessionState("testadvice", sessionId, { ...state, [field]: newest(map) });
}

/**
 * The edit-time lines for `files` (from `adviceFiles`), as `{ rel, text,
 * tests }` for each file that has one: `tests` counts the tests `text` names,
 * 0 when it names the functions the index links no test to. Each file is
 * asked about once per session, at its first change, so a function changed
 * later in the same file is not asked about. A file the server gave no
 * answer for is asked about once more, at the next call that names it, so a
 * server that is down costs a session two bounded requests per file and not
 * one per tool call. A file whose change is comments only is not asked
 * about and stays unasked: it owes no test until it changes code. A test is
 * named once per session: the test file a session edits after the source
 * file it tests adds no second line with the same command. A test the call's
 * own command runs is not named (`runsHere`): the line would arrive on the
 * result of that run. Only the diff's paths, hunk ranges and function
 * names are sent (`diffHeaders`).
 */
export async function adviceLines(payload, files, { root, repo, token, depth = gateDepth() } = {}) {
  const asked = files
    .map((rel) => [
      rel,
      git(["diff", ...DIFF_ARGS, "--unified=0", "HEAD", "--", rel], root, { timeoutMs: 2000, raw: true }),
    ])
    .filter(([, diff]) => !commentOnlyDiff(diff));
  if (!asked.length) return [];
  const gates = await Promise.all(
    asked.map(([rel, diff]) =>
      postBounded(
        "code.test_gate",
        {
          org: repo.org,
          namespace: repo.namespace,
          files: [rel],
          diff: diffHeaders(diff, 200_000),
          depth,
          consumer: "claude-code-testgate",
        },
        { token, serverUrl: resolveServerUrl() },
      ),
    ),
  );
  const state = readSessionState("testadvice", payload.session_id);
  const rels = asked.map(([rel]) => rel);
  // No answer (a timeout, a 5xx): the file stays unasked for one more call that names it.
  const again = rels.filter((rel, i) => !gates[i] && !(state.retried || {})[rel]);
  if (again.length) remember(payload.session_id, "retried", again);
  if (again.length < rels.length) {
    remember(
      payload.session_id,
      "seen",
      rels.filter((rel) => !again.includes(rel)),
    );
  }
  const named = { ...state.named };
  const command = commandText(payload.tool_input);
  const testExists = testsIn(root);
  let python;
  const lines = [];
  const printed = [];
  for (const [i, [rel]] of asked.entries()) {
    const tests = adviceTests(rel, gates[i], testExists);
    const fresh = tests.filter((t) => !named[t.test_path] && !(t.run && runsHere(command, String(t.run))));
    if (python === undefined && fresh.some((t) => isPytest(t.run))) python = checkoutPython(root);
    // Functions are named only for a file with no test to run in this checkout.
    const untested = tests.length ? "" : untestedNames(gates[i]).list;
    const text = renderAdvice(rel, fresh, { python, untested });
    if (!text) continue;
    // A test folded into "(+N more)" was not shown, so its own change still names it.
    for (const t of printedTests(fresh)) {
      named[t.test_path] = Date.now();
      printed.push(t.test_path);
    }
    lines.push({ rel, text, tests: fresh.length });
  }
  if (printed.length) remember(payload.session_id, "named", printed);
  return lines;
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
/** True when `command` runs `run`, a test's whole command, in this checkout:
 *  `pytest tests/test_a.py::test_one` runs one test of the file, and `echo
 *  "pytest tests/test_a.py"` and `docker exec app pytest tests/test_a.py` run
 *  nothing here. */
function runsHere(command, run) {
  const at = command.indexOf(run);
  return at >= 0 && /^(?:[\s;|&)]|$)/.test(command.slice(at + run.length)) && ranHere(command.slice(0, at));
}

// A shell command that starts a test runner, wrapped or piped in any way: a
// program `code.test_gate` names a run with or a common other one, or a
// build tool with an argument in which `test` or `tests` follows no letter
// (`make test-unit`, `make testall`, `npm run test`, `node --test`, `gradle
// :app:test`, `gradlew testDebugUnitTest`), or Gradle with a task in which
// `Test` follows a lower-case letter (`gradlew integrationTest`). `import
// pytest`, `pytest.raises`, `make latest` and `node scripts/attest.js` are
// not runs. A command that only mentions a runner counts: what is read is
// whether the session ran a test at all.
//
// After a package manager's verb earlier in the simple command the name is a
// package: `pip install -U pytest`, `npm i -D @types/jest`, `uv add --dev
// pytest`, `go get github.com/stretchr/testify`. A flag is no verb: `npx
// --no-install jest` runs.
const FETCHED = String.raw`\s(?:(?:un)?install|add|i|ci|get)\s[^\n;|&]*`;
// The task after Gradle's `-x` is left out of the build: `gradle build -x
// test`. Maven's `-Dmaven.test.skip=true` runs none either (`(?!\.skip)`).
const LEFT_OUT = String.raw`\s-x\s+\S*`;
const RUNNER = new RegExp(
  String.raw`(?<![\w.-])(?:(?<!import\s)(?:py\.?test|unittest|tox|nox|jest|vitest|mocha|nextest|rspec|phpunit|ctest|clickhouse-test)(?![\w.-])(?<!${FETCHED})` +
    String.raw`|(?:go|cargo|npm|pnpm|yarn|bun|deno|make|mvn|gradle|gradlew|dotnet|swift|bazel|mix|rake|node)\s[^\n;|&]*(?<![A-Za-z])tests?(?!\.skip)(?<!${FETCHED}|${LEFT_OUT})` +
    String.raw`|gradlew?\s[^\n;|&]*[a-z]Test(?<!${LEFT_OUT}))`,
);

/** True for the name of a Codex rollout's shell tool, under any of the names
 *  `normalizeToolName` folds onto Bash. */
const rolloutShell = (name) => name === "exec" || normalizeToolName(name) === "Bash";

/** The command text of a Codex rollout's shell call: its `arguments` JSON
 *  holds a string or an argv (`["go","test"]`), whose words no space joins. */
function rolloutCommand(call) {
  let input = call.input ?? call.arguments;
  if (typeof input === "string") {
    try {
      input = JSON.parse(input);
    } catch {
      return input;
    }
  }
  return commandText(input) || (typeof input === "string" ? input : "");
}

/**
 * True when the transcript holds a shell command that starts a test runner
 * (`RUNNER`) and finished at or after `since` (epoch ms), or has no dated
 * result: Claude Code rows and Codex rollout calls, sidechains skipped. Its
 * output is not read, so a run that failed or was piped into `grep` counts.
 * So does one between `git stash` and `git stash pop` in the same command; a
 * pop in a later command rewrites the changed files after the run.
 */
export function testedSince(objs, since) {
  const done = new Map();
  const runs = [];
  for (const o of objs || []) {
    const at = Date.parse((o && o.timestamp) || "");
    if (o?.type === "response_item") {
      const call = o.payload || {};
      if (/_output$/.test(call.type)) done.set(call.call_id, at);
      else if (rolloutShell(call.name) && RUNNER.test(rolloutCommand(call))) {
        runs.push(call.call_id);
      }
      continue;
    }
    const content = o && !o.isSidechain && o.message && o.message.content;
    for (const b of Array.isArray(content) ? content : []) {
      if (o.type === "user" && b?.type === "tool_result") done.set(b.tool_use_id, at);
      else if (
        o.type === "assistant" &&
        b?.type === "tool_use" &&
        ["Bash", "PowerShell"].includes(normalizeToolName(b.name)) &&
        RUNNER.test(commandText(b.input))
      ) {
        runs.push(b.id);
      }
    }
  }
  return runs.some((id) => !(done.get(id) < since));
}

// Documentation and prose by name: a test run answers no change to one.
// A `.txt` file is code: a requirements file, a build list, a test fixture.
const PROSE = /\.(?:md|mdx|markdown|rst|adoc|asciidoc)$/i;
const CHANGELOG = /(?:^|\/)(?:CHANGELOG|CHANGES|HISTORY|NEWS)$/;
const isProse = (file) => CHANGELOG.test(file) || PROSE.test(file);

/** The newest modification time, in epoch ms, among the code files of
 *  `files` under `root`: a test run that finished since then saw each of
 *  them as it stands. A documentation or prose file (`isProse`) is left out,
 *  so an edit to one after the run holds no stop, and so is a file that
 *  cannot be read (a deleted one). 0 when none is left. */
export function lastCodeChange(root, files) {
  let last = 0;
  for (const file of files) {
    if (isProse(file)) continue;
    try {
      last = Math.max(last, fs.statSync(path.join(root, file)).mtimeMs);
    } catch {
      // A deleted file has no time of its own.
    }
  }
  return last;
}

// pytest's last line, whose order is fixed: failed, passed, skipped,
// deselected, xfailed, xpassed, warnings, errors. A whole line starts at
// "failed" or "passed" and names nothing deselected, so a -k subset is not
// one, and neither is a collection that stopped at an error. --lf leaves
// tests out at collection and prints the line of a full run.
const SUMMARY = /\b\d+ (?:passed|failed|errors?)\b.* in [\d.]+s\b/;
const WHOLE =
  /^=*\s*\d+ (?:failed|passed)(?:, \d+ (?:passed|skipped|xfailed|xpassed|warnings?|errors?))* in [\d.]+s(?: \([\d:]+\))?\s*=*$/;
// pytest flags that end a run at a failure, before the files after it.
const STOPS = /(?:^|\s)(?:-[A-Za-z]*x|--exitfirst|--maxfail)/;
// The line pytest prints above the summary of a run it ended early: at a
// failure limit, at --sw, at a collection error. It shows a limit set in a
// configuration file or in PYTEST_ADDOPTS, which the command does not.
const ENDED = /^!{3,} .*(?:stopping after \d+ failures|Interrupted: ).* !{3,}$/;
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

/** True when the output holds a whole pytest summary for each of `runs`
 *  and no other summary: `| tail -5; echo exit=$?` hides the exit status,
 *  the summary line survives it. Failures count: the session has read
 *  them. No line is whole under pytest's line for a run it ended early,
 *  and one that counts a failure is not whole when a run has a flag that
 *  stops at one: `| tail -1` cuts pytest's line, the flag stays. */
function allRan(output, runs) {
  const all = String(output || "")
    .split(/\r?\n/)
    .map((l) => l.trim());
  const lines = all.filter((l) => l.length < 300 && SUMMARY.test(l));
  const stops = runs.some((run) => STOPS.test(run[1]));
  return (
    !all.some((l) => ENDED.test(l)) &&
    lines.length >= runs.length &&
    lines.every((l) => WHOLE.test(l) && !(stops && /\d (?:failed|error)/.test(l)))
  );
}

/**
 * The repo-relative files and directories pytest ran to its summary on the
 * tree as it stands, read in transcript order (Claude Code rows; sidechains
 * skipped). A run counts when its Bash command finished no earlier than
 * `since`, the newest time of an uncommitted or untracked file, so an edit
 * by a subagent or a script the transcript never shows forgets it. An edit
 * tool or any other shell command after it forgets every earlier run: it
 * may have deleted a file or put one back as HEAD has it, which leaves no
 * newer time. A command may change files and then test them
 * (`cat >> tests/test_a.py <<EOF … EOF; pytest tests`): its runs count from
 * the first one that only runs and output follow (`onlyRuns`), so in
 * `git stash; pytest tests; git stash pop; pytest tests` the second does.
 * A command that holds anything before that run but its launcher forgets
 * every earlier run. A run's paths are read from the directory it ran in:
 * the row's own cwd, moved by at most one `cd` before the first run.
 */
function wholeRuns(objs, { root, cwd, since } = {}) {
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
  let whole = [];
  for (const o of rows) {
    if (o.type !== "assistant") continue;
    for (const b of o.message.content) {
      if (!b || b.type !== "tool_use") continue;
      const name = normalizeToolName(b.name);
      if (EDIT_TOOLS.has(name)) whole = [];
      if (name !== "Bash" && name !== "PowerShell") continue;
      const command = commandText(b.input);
      // Bash only: this reader follows no PowerShell move (`Set-Location`).
      const runs = name === "Bash" ? [...command.matchAll(PYTEST)] : [];
      if (!runs.length) {
        whole = [];
        continue;
      }
      const cd = [...command.slice(0, runs[0].index).matchAll(CD)];
      // The runs that tested the tree the command left: the first one that
      // only runs and output follow, and the ones after it.
      const from = runs.findIndex((run) => onlyRuns(command.slice(run.index)));
      const before = from < 0 ? "" : command.slice(0, runs[from].index);
      const result = results.get(b.id) || {};
      // --ignore, --deselect, --lf and --sw leave out tests a summary does
      // not count, and --continue-on-collection-errors counts a named file
      // that failed to import as one error among the tests that ran; a
      // quoted argument may be a flag's value in several words; a move the
      // reader cannot follow (a second `cd`, one between two runs, one in a
      // subshell that closed before the run, `pushd`) leaves the paths
      // unknown.
      const ran =
        from >= 0 &&
        allRan(result.output, runs) &&
        result.at >= since &&
        runs.every((run) => !/["']/.test(run[1]) && ranHere(command.slice(0, run.index))) &&
        cd.length < 2 &&
        (command.match(MOVE) || []).length === cd.length &&
        cd.every((m) => !closesParen(before.slice(m.index + m[0].length))) &&
        !/--(?:ignore|deselect|lf|last-failed|sw|stepwise|continue-on-collection-errors)\b/.test(command);
      if (!ran) {
        whole = [];
        continue;
      }
      // What the command did before that run may have changed the tree the
      // earlier runs tested, with no newer file time to show it.
      if (!LAUNCHER.test(before)) whole = [];
      const dir = o.cwd || cwd;
      const base = cd.length ? path.resolve(dir || root || "", unquoted(cd[0][1])) : dir;
      for (const run of runs.slice(from)) {
        let value = false;
        for (const word of run[1].split(/\s+/)) {
          const flag = word.startsWith("-");
          const rel = flag || value ? "" : relativeToRoot(root, word, base);
          if (rel) whole.push(rel);
          value = flag && !word.includes("=") && !BARE.test(word);
        }
      }
    }
  }
  return whole;
}

/**
 * True when the gate's ask is to run tests this checkout has and the
 * transcript already answers it: the person's latest prompt ends by saying
 * not to run tests, or `wholeRuns` holds every named test (its file, or a
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
  const whole = wholeRuns(objs, { root, cwd, since: lastModified() });
  return paths.every((p) => whole.some((c) => p === c || p.startsWith(`${c}/`)));
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
