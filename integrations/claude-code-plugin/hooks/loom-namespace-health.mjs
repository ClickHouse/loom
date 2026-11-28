#!/usr/bin/env node
// loom-namespace-health — the session's namespace audit, told at a Stop.
//
// Once per session, ask `namespace.audit` (one day, no samples) how the
// session's namespace is being used and, when it carries a critical finding,
// hand the agent ONE line naming the codes and /loom-optimize — as a
// block-once reason, the one channel a Stop hook has to the model. See
// lib/namespace-health.mjs for the rules.
//
//   UserPromptSubmit  → asks, and keeps the verdict in the session's state.
//                       Claude Code runs this in loom-record's detached
//                       process, so no prompt and no Stop waits for the audit.
//   Stop              → tells a stored verdict once, with no call. A wiring
//                       that starts this script at Stop only (Codex, a
//                       hand-wired client) asks here, on the first Stop.
//
// Fail-open everywhere: no token, no scope, server down or slower than the
// hook's bound, a healthy namespace, a stop this hook caused — the agent
// stops as normal and nothing is printed. LOOM_NAMESPACE_HEALTH=off disables.

import {
  envSwitch,
  postBounded,
  readStdinJson,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { auditBody, decideAsk, decideAudit, healthPayload, renderHealth } from "../lib/namespace-health.mjs";
import { readJsonFile, sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";

const ENABLED = envSwitch("LOOM_NAMESPACE_HEALTH", true);

/** The line for the session's namespace: "" when there is nothing to say. */
async function verdict(payload, token) {
  const scope = resolveScope(String(payload.cwd || process.cwd()));
  if (!scope || !scope.namespace) return "";
  const report = await postBounded("namespace.audit", auditBody(scope), { token, serverUrl: resolveServerUrl() });
  return renderHealth(report, scope.namespace);
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
  const cp = sessionStatePath("nshealth", payload.session_id);

  if (process.argv[2] === "UserPromptSubmit") {
    const decision = decideAsk(readJsonFile(cp));
    if (decision.ask) {
      writeJsonFile(cp, decision.state);
      const line = await verdict(payload, token);
      // Read again: a Stop may have marked the session while this one asked.
      writeJsonFile(cp, { ...readJsonFile(cp), line });
    }
    scheduleExit(0);
    return;
  }

  // A Stop fired by our own block (or another gate's) must never block again.
  if (payload.stop_hook_active) {
    scheduleExit(0);
    return;
  }
  const state = readJsonFile(cp);
  const decision = decideAudit(state);
  if (decision.cache !== state) writeJsonFile(cp, decision.cache);
  const line = decision.audit ? await verdict(payload, token) : decision.line;
  const out = healthPayload(line);
  if (out) process.stdout.write(JSON.stringify(out) + "\n");
  scheduleExit(0);
}

runHookMain(main);
