#!/usr/bin/env node
// loom-cline: Cline CLI hooks onto the Claude Code plugin's hook scripts.
//
//   node loom-cline.mjs <UserPromptSubmit|PreToolUse>   (Cline's payload on stdin)
//
// What each hook does, and why only PreToolUse adds context: README.md. The plugin's
// scripts run unchanged as child processes (client name `cline`); every path prints
// `{"cancel": false, ...}` and exits 0.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  envSwitch,
  gitToplevel,
  readStdinJson,
  redactHead,
  resolveToken,
} from "../claude-code-plugin/lib/loom-client.mjs";
import { resolveIndexedRepo } from "../claude-code-plugin/lib/repo-scope.mjs";
import { readSessionState, writeSessionState } from "../claude-code-plugin/lib/tmp-state.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN = path.join(HERE, "..", "claude-code-plugin");
const CLIENT = "cline";
// Cline's file-editing tools, folded onto the plugin's names.
const EDIT_TOOLS = { editor: "Edit", write_to_file: "Write", replace_in_file: "Edit", apply_patch: "apply_patch" };
// A read's file blocks are looked up for its first files only.
const MAX_READ_FILES = 3;
// One child's wall: the plugin's own request bound is 3-8 s.
const CHILD_MS = 10_000;
// No file lookup starts after the hook has run this long.
const FILES_MS = 15_000;
// The prompt the recall reads is cut here, as the plugin's recall cuts it.
const PROMPT_CHARS = 2000;

/** Run one plugin hook script with `payload` on stdin; its stdout, "" on any failure. */
function runPluginHook(script, event, payload) {
  return new Promise((resolve) => {
    let out = "";
    const child = spawn(process.execPath, [path.join(PLUGIN, "hooks", script), event, CLIENT], {
      stdio: ["pipe", "pipe", "ignore"],
      // No sign-up from a Cline task unless asked for: the plugin's first-session
      // notice speaks of Claude Code.
      env: { ...process.env, LOOM_AUTO_SIGNUP: envSwitch("LOOM_AUTO_SIGNUP", false) ? "on" : "off" },
    });
    const timer = setTimeout(() => child.kill("SIGKILL"), CHILD_MS);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (c) => {
      out += c;
    });
    child.on("error", () => resolve(""));
    child.on("close", (_code, signal) => {
      clearTimeout(timer);
      resolve(signal ? "" : out); // a child killed at its bound may have printed half
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

/** The text a plugin hook gave the model: `hookSpecificOutput.additionalContext` of a
 *  JSON answer, else the plain text (the session primer prints text). */
export function contextOf(stdout) {
  const text = String(stdout || "").trim();
  if (!text) return "";
  if (text.startsWith("{")) {
    try {
      const obj = JSON.parse(text);
      return String(obj?.hookSpecificOutput?.additionalContext || "").trim();
    } catch {
      return "";
    }
  }
  return text;
}

/** The primer without its conversation-recording line (`recordingPrimerLine`, the one
 *  `# Loom: ` line `renderPrimer` ends with): it speaks of the plugin's Stop hook, which
 *  Cline does not run. Only a last line of that shape goes; recalled text stays whole. */
export function withoutRecordingLine(text) {
  const lines = String(text || "")
    .trimEnd()
    .split("\n");
  const last = lines[lines.length - 1] || "";
  if (last.startsWith("# Loom: ") && last.includes("loom_conversation_append")) lines.pop();
  return lines.join("\n").trim();
}

/** The plugin's payload fields from Cline's. */
export function basePayload(cline) {
  const root = cline?.workspaceInfo?.rootPath || (cline?.workspaceRoots || [])[0] || process.cwd();
  return { session_id: String(cline?.taskId || ""), cwd: String(root) };
}

/** A prompt the person wrote, not one of Cline's own continuation prompts. */
export function personPrompt(cline) {
  const p = String(cline?.userPromptSubmit?.prompt || "").trim();
  return p && !p.startsWith("[SYSTEM]") ? p : "";
}

function toolName(cline) {
  return String(cline?.preToolUse?.toolName || cline?.tool_call?.name || "");
}

function toolInput(cline) {
  return cline?.tool_call?.input || cline?.preToolUse?.parameters || {};
}

/** The plugin's (tool name, tool input) for a Cline file edit; null for any other tool. */
export function editCall(cline) {
  const mapped = EDIT_TOOLS[toolName(cline)];
  return mapped ? { tool_name: mapped, tool_input: toolInput(cline) } : null;
}

/** The files a `read_files` call reads, at most MAX_READ_FILES of them. Cline may send the
 *  list as JSON text. */
export function readPaths(cline) {
  if (toolName(cline) !== "read_files") return [];
  let files = toolInput(cline).files;
  if (typeof files === "string") {
    try {
      files = JSON.parse(files);
    } catch {
      return [];
    }
  }
  if (!Array.isArray(files)) return [];
  const paths = files
    .map((f) => String((typeof f === "string" ? f : f?.path) || ""))
    .filter(Boolean)
    .map((p) => path.posix.normalize(p));
  return [...new Set(paths)].slice(0, MAX_READ_FILES);
}

/** Fill the checkout's repository cache when it is stale: the file lookups read the team's
 *  file rules from it, and in Claude Code other hooks keep it fresh. Cache-or-lookup,
 *  bounded by the plugin's own lookup bound; a lookup that does not answer leaves an
 *  unresolved entry, retried after its short TTL. */
async function refreshRepoCache(cwd) {
  if (!resolveToken()) return; // no lookup to make, and no "not indexed" entry to leave
  const root = gitToplevel(cwd);
  if (root) await resolveIndexedRepo(root, { consumer: "cline-file-context" });
}

/** Whether the plugin's prompt recall runs: its own reading of LOOM_PROMPT_RECALL. */
function recallOn() {
  const mode = (process.env.LOOM_PROMPT_RECALL || "first").toLowerCase();
  return mode === "on" || mode === "first";
}

/** What a hook answers with no context, and nothing to mark. */
const NOTHING = Object.freeze({ context: "", prime: null });

/** The context for the model (`context`) and, on the task's first tool call, `prime`:
 *  the call that marks the task primed, for main to make once the answer is written. */
export async function handle(
  event,
  cline,
  { tmp = os.tmpdir(), run = runPluginHook, repo = refreshRepoCache, filesMs = FILES_MS, now = Date.now } = {},
) {
  const base = basePayload(cline);
  if (!base.session_id) return NOTHING;
  if (event === "UserPromptSubmit") {
    if (!recallOn()) return NOTHING; // nothing would read the prompt
    const prompt = personPrompt(cline);
    const state = readSessionState("cline", base.session_id, { tmp });
    if (prompt && !state.prompt) {
      // redacted before the cut, so a secret across it is still matched whole
      const kept = redactHead(prompt, PROMPT_CHARS).slice(0, PROMPT_CHARS);
      writeSessionState("cline", base.session_id, { ...state, prompt: kept }, { tmp });
    }
    return NOTHING;
  }
  if (event !== "PreToolUse") return NOTHING;
  const state = readSessionState("cline", base.session_id, { tmp });
  const parts = [];
  let prime = null;
  const started = now();
  if (!state.primed) {
    // The primer fills the checkout's repository cache the file lookups read for the
    // team's file rules, so they wait for it; the recall runs beside it.
    const [primer, recall] = await Promise.all([
      run("loom-session-start.mjs", "SessionStart", { ...base, source: "startup", hook_event_name: "SessionStart" }),
      state.prompt
        ? run("loom-prompt-recall.mjs", "UserPromptSubmit", {
            ...base,
            prompt: state.prompt,
            hook_event_name: "UserPromptSubmit",
          })
        : Promise.resolve(""),
    ]);
    // `prime` marks the task once main has written the answer: if Cline cuts the hook
    // short, the primer is tried again on the next tool call. The recall and file blocks
    // are not: their scripts mark themselves done before they print.
    prime = () => writeSessionState("cline", base.session_id, { ...state, primed: true }, { tmp });
    parts.push(withoutRecordingLine(contextOf(primer)), contextOf(recall));
  }
  let files = []; // with the file block's own switch off: no lookup and no cache refresh
  if (envSwitch("LOOM_FILE_CONTEXT", true)) {
    const edit = editCall(cline);
    files = edit ? [edit] : readPaths(cline).map((p) => ({ tool_name: "Edit", tool_input: { file_path: p } }));
  }
  if (files.length) await repo(base.cwd);
  // one at a time: the plugin's script keeps one shown-file state per task
  for (const call of files) {
    if (now() - started >= filesMs) break;
    parts.push(
      contextOf(await run("loom-file-context.mjs", "PreToolUse", { ...base, ...call, hook_event_name: "PreToolUse" })),
    );
  }
  return { context: parts.filter(Boolean).join("\n\n"), prime };
}

async function main() {
  const event = String(process.argv[2] || "");
  let found = NOTHING;
  try {
    found = await handle(event, await readStdinJson());
  } catch {
    found = NOTHING;
  }
  const { context, prime } = found;
  const answer = context ? { cancel: false, contextModification: context } : { cancel: false };
  // once the answer is written (a pipe's write can be asynchronous), mark the task and exit
  process.stdout.write(`${JSON.stringify(answer)}\n`, (err) => {
    if (!err && prime) prime(); // a failed write did not reach Cline
    process.exit(0);
  });
}

// Node runs the main module from its real path: compare real paths, so a LOOM_CLINE_DIR
// through a symlink still runs.
if (process.argv[1] && fs.realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
