#!/usr/bin/env node
// loom-mentor-hint — PostToolUse on the loom MCP tools.
//
// A successful loom tool result may carry one `hint` from the store's
// mentor — the plane this agent is not using, named from the call it just
// made (a memory.get that missed, a search that abstained where the code
// index answers, a stack trace written as a memory, a duplicate write). The
// model can read a tool result and skip a key; this hook hands the hint
// back as additionalContext so it is read as advice. Once per code per
// session, six per session, no request of its own, fail-open everywhere.
//
//   LOOM_MENTOR_HINTS=off   disable

import { envSwitch, planeEnabled, readStdinJson, runHookMain, scheduleExit } from "../lib/loom-client.mjs";
import { decideShow, extractHint, hintPayload, isLoomTool, renderHint } from "../lib/mentor-hint.mjs";
import { readJsonFile, sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";

// A hint names the plane the session is not using: in a one-plane mode that
// is the plane the install left out, so hints are for a session with both.
const ENABLED = planeEnabled("memory") && planeEnabled("code") && envSwitch("LOOM_MENTOR_HINTS", true);

async function main() {
  if (!ENABLED) {
    scheduleExit(0);
    return;
  }
  const payload = await readStdinJson();
  const toolName = payload.tool_name || "";
  if (!isLoomTool(toolName)) {
    scheduleExit(0);
    return;
  }
  const hint = extractHint(payload.tool_response);
  if (!hint) {
    scheduleExit(0);
    return;
  }

  const cp = sessionStatePath("mentor-hint", payload.session_id);
  const decision = decideShow(readJsonFile(cp), hint.code);
  if (!decision.show) {
    scheduleExit(0);
    return;
  }
  writeJsonFile(cp, decision.cache);

  const out = hintPayload(renderHint(hint, toolName));
  if (out) process.stdout.write(JSON.stringify(out) + "\n");
  scheduleExit(0);
}

runHookMain(main);
