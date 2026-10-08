// The turn journal — what the hooks themselves were told, kept per session.
//
// The Claude Code build of these hooks reads the session transcript (a JSONL
// file Claude Code names in every payload) to learn the user's prompts and
// the assistant's replies. Another coding agent may hand the hooks no
// transcript at all, or one in a shape these scripts do not parse; what it
// does hand them is the prompt on UserPromptSubmit and the reply on Stop
// (`last_assistant_message`). This module records those two facts as they
// arrive, so the consumers — conversation capture, the working-memory note,
// the run's final text, the session memory — read the same {role, content}
// turns whether or not a transcript exists.
//
// One JSONL file per session in tmpdir; bounded per line; never throws.
// Pure helpers take rows so they are unit-testable without I/O.

import fs from "node:fs";
import os from "node:os";
import { readCursor, sessionStatePath } from "./tmp-state.mjs";

export const JOURNAL_MAX_CHARS = 8000;
export const JOURNAL_MAX_ROWS = 400;

export function journalPath(sessionId, dir = os.tmpdir()) {
  return sessionStatePath("turns", sessionId, { ext: ".jsonl", tmp: dir });
}

export function journalCursorPath(sessionId, dir = os.tmpdir()) {
  return sessionStatePath("turns", sessionId, { ext: ".cursor", tmp: dir });
}

/** One journal row from a role + text, or null when there is nothing to keep. */
export function journalRow(role, content, at = new Date().toISOString()) {
  const text = String(content || "").trim();
  if (!text) return null;
  const r = role === "assistant" ? "assistant" : "user";
  return { role: r, content: text.length > JOURNAL_MAX_CHARS ? text.slice(0, JOURNAL_MAX_CHARS) : text, at };
}

/** Append one turn; true when a row was written. */
export function appendTurn(sessionId, role, content, { dir, at } = {}) {
  const row = journalRow(role, content, at);
  if (!row || !sessionId) return false;
  try {
    fs.appendFileSync(journalPath(sessionId, dir), JSON.stringify(row) + "\n");
    return true;
  } catch {
    return false;
  }
}

/** Parse journal text into rows; malformed lines are skipped, `i` is the
 *  0-based line index (the cursor unit). */
export function parseJournal(text) {
  const out = [];
  const lines = String(text || "").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i].trim();
    if (!raw) continue;
    try {
      const o = JSON.parse(raw);
      if (o && (o.role === "user" || o.role === "assistant") && typeof o.content === "string") {
        out.push({ role: o.role, content: o.content, at: typeof o.at === "string" ? o.at : "", i });
      }
    } catch {
      /* skip */
    }
  }
  return out.slice(-JOURNAL_MAX_ROWS);
}

export function readJournal(sessionId, { dir } = {}) {
  try {
    return parseJournal(fs.readFileSync(journalPath(sessionId, dir), "utf8"));
  } catch {
    return [];
  }
}

export function readJournalCursor(sessionId, { dir } = {}) {
  return readCursor(journalCursorPath(sessionId, dir));
}

/** Rows whose line index is at or past `cursor`. */
export function rowsSince(rows, cursor) {
  return (rows || []).filter((r) => r.i >= (cursor || 0));
}

/** {role, content, timestamp} turns in the shape the transcript readers produce. */
export function journalTurns(rows) {
  return (rows || []).map((r) => ({ role: r.role, content: r.content, timestamp: r.at }));
}

export function firstUser(rows) {
  const r = (rows || []).find((x) => x.role === "user");
  return r ? r.content : "";
}

export function lastAssistant(rows) {
  for (let k = (rows || []).length - 1; k >= 0; k--) if (rows[k].role === "assistant") return rows[k].content;
  return "";
}
