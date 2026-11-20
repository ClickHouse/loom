// Response observation precedes the caller's rendering and automatic callbacks.
// A binding is application-owned; it is never selected by wire arguments.

import { AsyncLocalStorage } from "node:async_hooks";

import { protectSession, sessionIsProtected, SessionProtectionUnavailable } from "./session-protection.mjs";

const context = new AsyncLocalStorage();
const MAX_ENVELOPES = 10000;
const MAX_TEXT_BYTES = 4 * 1024 * 1024;

export function sessionProtectionContext() {
  return context.getStore();
}

/** Automatic work never treats missing lineage as a clean family. */
export function automaticSessionIsProtected() {
  const roots = context.getStore()?.roots;
  return !roots?.length || roots.some(sessionIsProtected);
}

function retainedRoots(roots) {
  const known = new Map();
  for (const root of roots) {
    const copy = Object.freeze({ base: root?.base, id: root?.id });
    known.set(JSON.stringify([copy.base, copy.id]), copy);
  }
  return Object.freeze([...known.values()]);
}

function joinedRoots(root) {
  return retainedRoots([...(context.getStore()?.roots ?? []), root]);
}

/** Explicit operations remain available while retaining every response observer. */
export function withSessionProtection(root, callback) {
  return context.run(Object.freeze({ roots: joinedRoots(root), automatic: false }), callback);
}

/** A detached producer resumes its own frozen control, independent of its flusher. */
export function withCapturedSessionProtection(roots, callback) {
  const retained = retainedRoots(roots);
  if (!retained.length || retained.some(sessionIsProtected)) return undefined;
  return context.run(Object.freeze({ roots: retained, automatic: true }), callback);
}

/** Nested automatic callbacks cannot drop any contributing parent's protection. */
export function withAutomaticSessionProtection(root, callback) {
  return withCapturedSessionProtection(joinedRoots(root), callback);
}

function markedEnvelope(value) {
  const pending = [value];
  const visited = new Set();
  let remaining = MAX_ENVELOPES;
  let textBytes = MAX_TEXT_BYTES;
  while (pending.length) {
    if (--remaining < 0) throw new SessionProtectionUnavailable();
    const item = pending.pop();
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    if (visited.has(item)) throw new SessionProtectionUnavailable();
    visited.add(item);
    if (Object.hasOwn(item, "mirrored") && typeof item.mirrored !== "boolean") {
      throw new SessionProtectionUnavailable();
    }
    if (item.mirrored === true) return true;
    for (const key of ["result", "structuredContent"]) {
      if (item[key] && typeof item[key] === "object") pending.push(item[key]);
    }
    // JSON-RPC failures retain response metadata in error.data after unwrapping.
    if (item.error && typeof item.error === "object" && item.error.data && typeof item.error.data === "object") {
      pending.push(item.error.data);
    }
    if (Array.isArray(item.items)) {
      if (pending.length + item.items.length > remaining) throw new SessionProtectionUnavailable();
      pending.push(...item.items);
    }
    if (Array.isArray(item.content)) {
      if (pending.length + item.content.length > remaining) throw new SessionProtectionUnavailable();
      remaining -= item.content.length;
      for (const content of item.content) {
        if (content?.type !== "text" || typeof content.text !== "string") continue;
        // Charge every block before parsing; repeated source text must not
        // allocate a separate unbounded decoded object for each occurrence.
        if (content.text.length > textBytes) throw new SessionProtectionUnavailable();
        textBytes -= Buffer.byteLength(content.text, "utf8");
        if (textBytes < 0) throw new SessionProtectionUnavailable();
        const text = content.text.trim();
        if (!text.startsWith("{")) continue;
        try {
          pending.push(JSON.parse(text));
        } catch {
          throw new SessionProtectionUnavailable();
        }
      }
    }
  }
  return false;
}

function markEveryFamily(roots) {
  let failed = false;
  for (const root of roots) {
    try {
      protectSession(root);
    } catch {
      failed = true;
    }
  }
  return failed;
}

/** Unknown response state protects every known family before withholding output. */
export function refuseSessionResponse(roots) {
  markEveryFamily(roots);
  throw new SessionProtectionUnavailable();
}

/** Parse once and attempt every durable mark, including after another mark fails. */
export function observeSessionResponses(roots, value) {
  let marked;
  try {
    marked = markedEnvelope(value);
  } catch {
    return refuseSessionResponse(roots);
  }
  if (marked && markEveryFamily(roots)) throw new SessionProtectionUnavailable();
  return value;
}

/** Consume only response envelopes; arbitrary arguments cannot choose a binding. */
export function observeSessionResponse(root, value) {
  return observeSessionResponses(joinedRoots(root), value);
}
