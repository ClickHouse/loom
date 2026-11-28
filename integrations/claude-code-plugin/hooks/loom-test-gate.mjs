#!/usr/bin/env node
// loom-test-gate — PostToolUse (Bash|PowerShell) and Stop.
//
// PostToolUse, the default (LOOM_TEST_GATE unset): after a shell command
// that changed files in a loom-indexed repo, ask `code.test_gate` about each
// one and attach ONE line per file naming the command of the tests the
// index links to it, or of the file itself when it is a test, or the changed
// functions the index links no test to when there is no test to name; once
// per file and once per test per session. The line rides the tool result, so it
// costs no model request, and it arrives while the file is still being
// edited. An edit tool's call gets the same line from loom-blast-radius, the
// one hook wired for those tools. The stop is never held: whether the tests
// then ran is the agent's to judge.
//
// Stop, with LOOM_TEST_GATE=block: the gate holds the stop instead, and
// names nothing at edit time. If the working tree has uncommitted changes
// THIS SESSION made
// in a loom-indexed repo, ask `code.test_gate` for the change's obligations
// (tests to run, untested touched functions) and block the stop ONCE per
// distinct change with a reason that names them. "This session's" is read
// from the transcript: dirty paths an edit tool targeted or a shell command
// named, written since the session began (by the file's modification time);
// a tree left dirty before the session does not gate a turn that changed
// nothing. A Claude Code transcript that already answers the ask does not
// block either (`runAnswered` in lib/test-gate.mjs); a Codex rollout answers
// nothing, so its stop blocks.
//
// Fail-open everywhere: no token, not an indexed repo, clean tree, nothing
// of the tree touched this session, server down, second stop on the same
// change — the agent stops as normal. Without a transcript path the whole
// dirty tree is the change.
// LOOM_TEST_GATE=off disables both; LOOM_TEST_GATE_DEPTH (1-3) sets the hops.

import crypto from "node:crypto";
import {
  git,
  gitToplevel,
  normalizeToolName,
  postBounded,
  readStdinJson,
  DIFF_ARGS,
  diffHeaders,
  resolveServerUrl,
  resolveToken,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { cachedUnindexed, resolveIndexedRepo } from "../lib/repo-scope.mjs";
import { readSessionState, writeSessionState } from "../lib/tmp-state.mjs";
import { readAllLines } from "../lib/transcript.mjs";
import {
  adviceFiles,
  adviceLines,
  advicePayload,
  checkoutPython,
  commentOnlyDiff,
  decideBlock,
  gateDepth,
  gateMode,
  gatePayload,
  gateSignature,
  newestChange,
  renderGate,
  runAnswered,
  sessionScope,
  sessionStart,
  testsIn,
  writtenSince,
} from "../lib/test-gate.mjs";

const MODE = gateMode();
const DEPTH = gateDepth();
const AFTER_TOOL = process.argv[2] === "PostToolUse";

/** After a shell command: the line for the files it changed, or nothing. */
async function advise(payload, token) {
  const tool = normalizeToolName(payload.tool_name);
  if (tool !== "Bash" && tool !== "PowerShell") return;
  const cwd = payload.cwd || process.cwd();
  // This runs after every shell command: a checkout already known to have no index costs no git call.
  if (cachedUnindexed(cwd)) return;
  const { root, files } = adviceFiles(payload, { cwd });
  if (!files.length) return;
  const repo = await resolveIndexedRepo(root, { consumer: "claude-code-testgate" });
  if (!repo.indexed) return;
  const lines = await adviceLines(payload, files, { root, repo, token, depth: DEPTH });
  const out = advicePayload(lines.map((l) => l.text).join("\n"));
  if (out) process.stdout.write(JSON.stringify(out) + "\n");
}

async function main() {
  if (MODE !== (AFTER_TOOL ? "advise" : "block")) {
    scheduleExit(0);
    return;
  }
  const token = resolveToken();
  if (!token) {
    scheduleExit(0);
    return;
  }
  const payload = await readStdinJson();
  if (AFTER_TOOL) {
    await advise(payload, token);
    scheduleExit(0);
    return;
  }
  // A Stop fired by our own block must never block again.
  if (payload.stop_hook_active) {
    scheduleExit(0);
    return;
  }

  const cwd = payload.cwd || process.cwd();
  const root = gitToplevel(cwd);
  if (!root) {
    scheduleExit(0);
    return;
  }
  // -z: paths as the file system names them, where git would quote one.
  const dirty = git(["diff", "--name-only", "-z", "HEAD"], root, { timeoutMs: 2000, raw: true })
    .split("\0")
    .filter(Boolean);
  if (!dirty.length) {
    scheduleExit(0);
    return;
  }
  // Only what this session touched; no transcript → the whole dirty tree.
  const transcript = payload.transcript_path || "";
  const rows = transcript ? readAllLines(transcript) : [];
  const changed = transcript ? writtenSince(root, sessionScope(dirty, rows, { root, cwd }), sessionStart(rows)) : dirty;
  if (!changed.length) {
    scheduleExit(0);
    return;
  }
  const repo = await resolveIndexedRepo(root, { consumer: "claude-code-testgate" });
  if (!repo.indexed) {
    scheduleExit(0);
    return;
  }

  const diff = git(["diff", ...DIFF_ARGS, "--unified=0", "HEAD", "--", ...changed], root, {
    timeoutMs: 2000,
    raw: true,
  });
  // A comment- or whitespace-only change owes no test.
  if (commentOnlyDiff(diff)) {
    scheduleExit(0);
    return;
  }
  const diffHash = crypto.createHash("sha1").update(diff).digest("hex").slice(0, 16);
  const decision = decideBlock(readSessionState("testgate", payload.session_id), gateSignature(changed, diffHash));
  if (!decision.block) {
    scheduleExit(0);
    return;
  }

  const gate = await postBounded(
    "code.test_gate",
    {
      org: repo.org,
      namespace: repo.namespace,
      files: changed.slice(0, 50),
      diff: diffHeaders(diff, 200_000),
      depth: DEPTH,
      consumer: "claude-code-testgate",
    },
    { token, serverUrl: resolveServerUrl() },
  );
  const testExists = testsIn(root);
  // The newest file time among the uncommitted and untracked files.
  const lastModified = () => {
    const untracked = git(["ls-files", "-z", "--others", "--exclude-standard"], root, {
      timeoutMs: 2000,
      raw: true,
      fallback: null,
    });
    return newestChange(root, untracked === null ? null : [...dirty, ...untracked.split("\0").filter(Boolean)]);
  };
  if (runAnswered(gate, rows, { root, cwd, testExists, lastModified })) {
    scheduleExit(0);
    return;
  }
  // An answered stop returned above, so it keeps the change's one block.
  writeSessionState("testgate", payload.session_id, decision.cache);
  const needsPython = (gate?.tests || []).some((t) => /^pytest(\s|$)/.test(String(t?.run || "")));
  const out = gatePayload(renderGate(gate, { python: needsPython ? checkoutPython(root) : undefined, testExists }));
  if (out) process.stdout.write(JSON.stringify(out) + "\n");
  scheduleExit(0);
}

main().catch(() => scheduleExit(0));
