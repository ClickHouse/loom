#!/usr/bin/env node
// loom-test-gate — Stop hook.
//
// On Stop, if the working tree has uncommitted changes THIS SESSION made
// in a loom-indexed repo, ask `code.test_gate` for the change's obligations
// (tests to run, untested touched functions) and block the stop ONCE per
// distinct change with a reason that names them. "This session's" is read
// from the transcript: dirty paths an edit tool targeted or a shell command
// named; a tree left dirty before the session does not
// gate a turn that touched nothing. A Claude Code transcript that already
// answers the ask does not block either (`runAnswered` in lib/test-gate.mjs);
// a Codex rollout answers nothing, so its stop blocks.
//
// Fail-open everywhere: no token, not an indexed repo, clean tree, nothing
// of the tree touched this session, server down, second stop on the same
// change — the agent stops as normal. Without a transcript path the whole
// dirty tree is the change.
// LOOM_TEST_GATE=off disables; LOOM_TEST_GATE_DEPTH (1-3) sets the hops.

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
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
  checkoutPython,
  commentOnlyDiff,
  decideBlock,
  gatePayload,
  gateSignature,
  renderGate,
  runAnswered,
  sessionScope,
} from "../lib/test-gate.mjs";

const ENABLED = envSwitch("LOOM_TEST_GATE", true);
const DEPTH = Math.min(3, Math.max(1, Number(process.env.LOOM_TEST_GATE_DEPTH || 2)));
const PLAIN = ["-c", "core.quotePath=false"];

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
  // Paths as the file system names them: git quotes a non-ASCII one otherwise.
  const dirty = git([...PLAIN, "diff", "--name-only", "HEAD"], root, { timeoutMs: 2000 })
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!dirty.length) {
    scheduleExit(0);
    return;
  }
  // Only what this session touched; no transcript → the whole dirty tree.
  const transcript = payload.transcript_path || "";
  const rows = transcript ? readAllLines(transcript) : [];
  const changed = transcript ? sessionScope(dirty, rows, { root, cwd }) : dirty;
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
  const testExists = (testPath) => {
    const target = path.resolve(root, testPath);
    if (!pathUnder(root, target)) return false;
    try {
      return fs.statSync(target).isFile();
    } catch {
      // A stale index row or another checkout's test is an explicit gap.
      return false;
    }
  };
  // When the tree last changed: the newest mtime of an uncommitted or
  // untracked file. Infinity when one cannot be read (a deleted file, a
  // listing that failed), so that no test run counts as later.
  const lastModified = () => {
    const untracked = git([...PLAIN, "ls-files", "--others", "--exclude-standard"], root, {
      timeoutMs: 2000,
      fallback: null,
    });
    if (untracked === null) return Infinity;
    let last = 0;
    try {
      for (const file of [...dirty, ...untracked.split("\n").filter(Boolean)]) {
        last = Math.max(last, fs.statSync(path.join(root, file)).mtimeMs);
      }
    } catch {
      return Infinity;
    }
    return last;
  };
  if (runAnswered(gate, rows, { changed, root, cwd, testExists, lastModified })) {
    scheduleExit(0);
    return;
  }
  // Saved only now: a stop that showed nothing has not used the change's one block.
  writeSessionState("testgate", payload.session_id, decision.cache);
  const needsPython = (gate?.tests || []).some((t) => /^pytest(\s|$)/.test(String(t?.run || "")));
  const out = gatePayload(renderGate(gate, { python: needsPython ? checkoutPython(root) : undefined, testExists }));
  if (out) process.stdout.write(JSON.stringify(out) + "\n");
  scheduleExit(0);
}

main().catch(() => scheduleExit(0));
