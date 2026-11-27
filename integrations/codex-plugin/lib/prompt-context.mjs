import { basename, dirname, resolve } from "node:path";

// Caller metadata describes hook requests even when the host supplies no prompt identity.
const ID = /^[a-zA-Z0-9_.:-]{1,128}$/;
const valid = (value) => typeof value === "string" && ID.test(value) && !/[\r\n]/.test(value);

export function promptHeaders(payload = {}) {
  const headers = { "x-loom-call-origin": "hook", "x-loom-caller-type": "plugin" };
  const { session_id: session, prompt_id: prompt, turn_id: turn } = payload;
  if (valid(session)) headers["x-loom-conversation-id"] = session;
  if (!(prompt && turn && prompt !== turn) && valid(prompt || turn)) {
    headers["x-loom-prompt-id"] = prompt || turn;
  }
  if (valid(payload.tool_use_id)) headers["x-loom-tool-call-id"] = payload.tool_use_id;
  if (valid(payload.hook_event_name)) headers["x-loom-hook-event"] = payload.hook_event_name;
  return headers;
}

export function entrypointHeaders(entrypoint, hookDirectory) {
  if (typeof entrypoint !== "string" || dirname(resolve(entrypoint)) !== resolve(hookDirectory)) return {};
  const name = basename(entrypoint, ".mjs");
  if (!/^loom-[a-z-]+$/.test(name)) return {};
  return { "x-loom-call-origin": "hook", "x-loom-caller-type": "plugin", "x-loom-hook-name": name };
}
