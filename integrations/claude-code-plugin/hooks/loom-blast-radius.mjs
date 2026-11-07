#!/usr/bin/env node
// loom-blast-radius — PostToolUse (Edit|Write|MultiEdit).
//
// After the agent edits a file in a loom-indexed repository, diff that file
// against HEAD, ask `code.impact` (diff shape, depth 1) what the touched
// symbols reach, and inject ONE line: callers, tests reaching the change, and
// the co-change partners this session has not touched. The line lands while
// the agent is still in the edit, which is when a missed sibling file is
// cheap to fix.
//
// Fail-open everywhere: no token, not an indexed repo, untracked file, empty
// diff, server slow → nothing is printed. One bounded request (3 s). The same
// touched-symbol set for a file is reported once per session.
//
//   LOOM_BLAST_RADIUS=off        disable
//   LOOM_BLAST_MAX_NAMES=5       callers named inline
//   LOOM_BLAST_DEPTH=1           impact depth (1 or 2)
//   LOOM_BLAST_PER_HOP=50        impacted rows requested per hop (counts stay exact)

import fs from "node:fs";
import path from "node:path";
import {
  git,
  gitToplevel,
  postBounded,
  readStdinJson,
  DIFF_ARGS,
  diffHeaders,
  resolveServerUrl,
  resolveToken,
  scheduleExit,
  envSwitch,
} from "../lib/loom-client.mjs";
import { relativeForLookup, targetPath } from "../lib/file-context.mjs";
import { resolveIndexedRepo } from "../lib/repo-scope.mjs";
import { readSessionState, writeSessionState } from "../lib/tmp-state.mjs";
import { blastPayload, blastSignature, decideEmit, renderBlast } from "../lib/blast-radius.mjs";

const ENABLED = envSwitch("LOOM_BLAST_RADIUS", true);
const MAX_NAMES = Number(process.env.LOOM_BLAST_MAX_NAMES || 5);
const DEPTH = Math.min(2, Math.max(1, Number(process.env.LOOM_BLAST_DEPTH || 1)));
const PER_HOP = Math.min(500, Math.max(1, Number(process.env.LOOM_BLAST_PER_HOP || 50)));
const TOOLS = new Set(["Edit", "Write", "MultiEdit", "apply_patch"]);

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
  if (!TOOLS.has(String(payload.tool_name || ""))) {
    scheduleExit(0);
    return;
  }
  const file = targetPath(payload.tool_name, payload.tool_input);
  if (!file) {
    scheduleExit(0);
    return;
  }

  const cwd = payload.cwd || process.cwd();
  const root = gitToplevel(cwd);
  if (!root) {
    scheduleExit(0);
    return;
  }
  const rel = relativeForLookup(root, file, cwd);
  if (!rel || (rel === path.basename(file) && !fs.existsSync(path.join(root, rel)))) {
    scheduleExit(0);
    return;
  }

  const repo = await resolveIndexedRepo(root, { consumer: "claude-code-blast" });
  if (!repo.indexed) {
    scheduleExit(0);
    return;
  }

  // The session's edits to this file, as the index will see them: post-image
  // line numbers, no context lines to subtract. Untracked = nothing depends
  // on it yet; empty = the edit was a no-op or already committed. The outline is
  // sent in its place; with no `+++` path in it (a mode-only change, a binary file,
  // a header dropped for a line end) the server has nothing to read.
  const diff = git(["diff", ...DIFF_ARGS, "--unified=0", "HEAD", "--", rel], root, {
    timeoutMs: 2000,
    raw: true,
  });
  const outline = diffHeaders(diff, 200_000);
  if (!/^\+\+\+ /m.test(outline)) {
    scheduleExit(0);
    return;
  }

  const impact = await postBounded(
    "code.impact",
    // per_hop_limit keeps a hub's fan-out to one page; by_hop_counts still
    // carries the true total, so one round trip says both.
    {
      org: repo.org,
      namespace: repo.namespace,
      diff: outline,
      depth: DEPTH,
      per_hop_limit: PER_HOP,
      consumer: "claude-code-blast",
    },
    { token, serverUrl: resolveServerUrl() },
  );
  if (!impact) {
    scheduleExit(0);
    return;
  }

  const touchedPaths = git(["diff", "--name-only", "HEAD"], root, { timeoutMs: 2000 })
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  const decision = decideEmit(readSessionState("blast", payload.session_id), rel, blastSignature(rel, impact));
  writeSessionState("blast", payload.session_id, decision.cache);
  if (!decision.emit) {
    scheduleExit(0);
    return;
  }

  const text = renderBlast(rel, impact, { maxNames: MAX_NAMES, touchedPaths });
  if (text) process.stdout.write(JSON.stringify(blastPayload(text)) + "\n");
  scheduleExit(0);
}

main().catch(() => scheduleExit(0));
