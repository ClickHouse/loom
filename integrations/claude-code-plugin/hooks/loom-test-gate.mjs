#!/usr/bin/env node
// loom-test-gate — Stop hook.
//
// On Stop, if the working tree has uncommitted changes THIS SESSION made
// in a loom-indexed repo, ask `code.test_gate` for the change's obligations
// (tests to run, untested touched functions) and block the stop ONCE per
// distinct change with a reason that names them. "This session's" is read
// from the transcript: dirty paths an edit tool targeted or a shell command
// named; a tree left dirty before the session does not
// gate a turn that touched nothing. See lib/test-gate.mjs for the rules.
//
// Fail-open everywhere: no token, not an indexed repo, clean tree, nothing
// of the tree touched this session, server down, second stop on the same
// change — the agent stops as normal. Without a transcript path the whole
// dirty tree is the change.
// LOOM_TEST_GATE=off disables; LOOM_TEST_GATE_DEPTH (1-3) sets the hops.

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  git,
  gitToplevel,
  pathUnder,
  postBounded,
  readStdinJson,
  DIFF_ARGS,
  diffHeaders,
  resolveServerUrl,
  resolveToken,
  scheduleExit,
  envSwitch,
} from "../lib/loom-client.mjs";
import { resolveIndexedRepo } from "../lib/repo-scope.mjs";
import { readSessionState, writeSessionState } from "../lib/tmp-state.mjs";
import { readAllLines } from "../lib/transcript.mjs";
import {
  venvPython,
  commentOnlyDiff,
  decideBlock,
  gatePayload,
  gateSignature,
  renderGate,
  sessionScope,
} from "../lib/test-gate.mjs";

const ENABLED = envSwitch("LOOM_TEST_GATE", true);
const DEPTH = Math.min(3, Math.max(1, Number(process.env.LOOM_TEST_GATE_DEPTH || 2)));

/** The interpreter that runs this checkout's pytest: its own virtualenv
 *  first, then an active one, then a PATH python that imports pytest; ""
 *  when none does, so the gate can say there is none instead of sending the
 *  agent to look. */
function checkoutPython(root) {
  const cands = [
    venvPython(path.join(root, ".venv")),
    venvPython(path.join(root, "venv")),
    process.env.VIRTUAL_ENV ? venvPython(process.env.VIRTUAL_ENV) : "",
  ].filter(Boolean);
  for (const c of cands) {
    try {
      if (fs.statSync(c).isFile()) return pathUnder(root, c) ?? c;
    } catch {
      /* next */
    }
  }
  for (const py of ["python3", "python"]) {
    try {
      execFileSync(py, ["-c", "import pytest"], { cwd: root, timeout: 1500, stdio: "ignore", windowsHide: true });
      return py;
    } catch {
      /* next */
    }
  }
  return "";
}

async function main() {
  if (!ENABLED) {
    scheduleExit(0);
    return;
  }
  const token = resolveToken();
  if (!token) {
    scheduleExit(0);
    return;
  }
  const payload = await readStdinJson();
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
  const dirty = git(["diff", "--name-only", "HEAD"], root, { timeoutMs: 2000 })
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!dirty.length) {
    scheduleExit(0);
    return;
  }
  // Only what this session touched; no transcript → the whole dirty tree.
  const transcript = payload.transcript_path || "";
  const changed = transcript ? sessionScope(dirty, readAllLines(transcript), { root, cwd }) : dirty;
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
  writeSessionState("testgate", payload.session_id, decision.cache);
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
  const needsPython = (gate?.tests || []).some((t) => /^pytest(\s|$)/.test(String(t?.run || "")));
  const out = gatePayload(
    renderGate(gate, {
      python: needsPython ? checkoutPython(root) : undefined,
      testExists: (testPath) => {
        const target = path.resolve(root, testPath);
        if (!pathUnder(root, target)) return false;
        try {
          return fs.statSync(target).isFile();
        } catch {
          // A stale index row or another checkout's test is an explicit gap.
          return false;
        }
      },
    }),
  );
  if (out) process.stdout.write(JSON.stringify(out) + "\n");
  scheduleExit(0);
}

main().catch(() => scheduleExit(0));
