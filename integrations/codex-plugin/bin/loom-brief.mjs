#!/usr/bin/env node
// Session briefs from the plugin: list, load or save the
// condensed state of a line of work. The loom-session skill runs this when the
// person asks to resume or save; nothing here runs on its own.
//
//   node loom-brief.mjs list [--all]
//       numbered briefs for this project (--all: every brief in the namespace)
//   node loom-brief.mjs get [N | KEY] [--session SID] [--revision N] [--transcript [--turns N]]
//       one brief, chosen by its number in `list`, its key, or a session that
//       wrote it (default: this checkout's auto/<project>/<branch>), at one of
//       two depths:
//         (default)     the brief (goal, status, next steps, decisions, …) and
//                       the client's own summary from its last compaction
//         --transcript  + every recorded turn of the brief's sessions (full
//                       restoration), and `claude --resume <sid>` when the
//                       session's transcript is on this machine
//   node loom-brief.mjs save [KEY] < sections.json
//       save sections you wrote (goal, status, decisions, …)

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { git as gitIn, postBounded, resolveScope, resolveServerUrl, resolveToken } from "../lib/loom-client.mjs";
import { ago, autoBriefKey } from "../lib/session-brief.mjs";

const TIMEOUT_MS = 30000;
const LIST_LIMIT = 20;
const PAGE = 1000;
const DEFAULT_TURNS = 2000;

const git = (args, opts = {}) => gitIn(args, process.cwd(), { timeoutMs: 3000, ...opts });

export function parseArgs(argv) {
  const out = {
    cmd: argv[0] || "get",
    key: "",
    revision: 0,
    all: false,
    transcript: false,
    session: "",
    turns: DEFAULT_TURNS,
  };
  for (let i = 1; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--revision") out.revision = Number(argv[++i]) || 0;
    else if (a === "--all") out.all = true;
    else if (a === "--transcript") out.transcript = true;
    else if (a === "--session") out.session = String(argv[++i] || "");
    else if (a === "--turns") out.turns = Math.max(0, Number(argv[++i]) || 0);
    else if (!out.key && !a.startsWith("--")) out.key = a;
  }
  return out;
}

/** A bare 1-2 digit selector is a number from `list`, not a key. */
export function isListNumber(sel) {
  return /^\d{1,2}$/.test(String(sel || ""));
}

/** The list as the person reads it: numbered, newest first, one brief per
 *  two lines, and how to pick one. */
export function formatList(briefs, { scopeNote = "" } = {}) {
  if (!briefs.length) return `No saved session briefs${scopeNote}.\n`;
  const lines = [`Saved session briefs${scopeNote}, newest first:`];
  briefs.forEach((b, i) => {
    const where = [b.branch ? `branch ${b.branch}` : "", b.agent || ""].filter(Boolean).join(", ");
    lines.push(`${i + 1}. ${b.brief_key} — rev ${b.revision}, ${ago(b.age_seconds || 0)}${where ? `, ${where}` : ""}`);
    const what = b.goal || b.status || "";
    if (what) lines.push(`   ${what}`);
  });
  lines.push(
    "",
    "Load one: get <number|key> — the brief and the client's last compaction summary; " +
      "add --transcript for every recorded turn (full restoration).",
  );
  return `${lines.join("\n")}\n`;
}

/** Where Claude Code keeps a session's transcript on this machine, or "". */
export function localTranscript(sessionId, home = homedir()) {
  if (!/^[\w-]{8,}$/.test(String(sessionId || ""))) return "";
  const root = join(home, ".claude", "projects");
  let dirs = [];
  try {
    dirs = readdirSync(root);
  } catch {
    return "";
  }
  for (const d of dirs) {
    const p = join(root, d, `${sessionId}.jsonl`);
    if (existsSync(p)) return p;
  }
  return "";
}

/** One session's recorded turns as a readable replay. */
export function renderTranscript(sessionId, messages, { total = 0, capped = false } = {}) {
  const n = messages.length;
  const head =
    `## Session ${sessionId} — ${n} recorded turn${n === 1 ? "" : "s"}` +
    (capped ? ` (the last ${n} of ${total}; --turns 0 for all)` : "");
  const body = messages.map((m) => `[${m.role}] ${String(m.content || "").trim()}`);
  return [head, ...body].join("\n\n");
}

async function recordedTurns(post, sessionId, maxTurns) {
  const first = await post("conversation.get", {
    thread_id: sessionId,
    include_messages: true,
    limit: 1,
  });
  if (!first || !first.found) return { found: false, messages: [], total: 0 };
  const total = Number(first.msg_count || 0);
  const want = maxTurns > 0 ? Math.min(total, maxTurns) : total;
  const skip = total - want; // keep the newest `want` turns, in order
  const messages = [];
  for (let off = skip; off < total; off += PAGE) {
    const page = await post("conversation.get", {
      thread_id: sessionId,
      include_messages: true,
      limit: Math.min(PAGE, total - off),
      offset: off,
    });
    if (!page || !Array.isArray(page.messages) || !page.messages.length) break;
    messages.push(...page.messages);
  }
  return { found: true, messages, total, capped: want < total };
}

async function resolveKey(args, post, scope, branch) {
  if (args.key && !isListNumber(args.key)) return { key: args.key };
  if (args.key) {
    const r = await post("session.brief_list", { project: args.all ? "" : scope.project, limit: LIST_LIMIT });
    const briefs = r && Array.isArray(r.briefs) ? r.briefs : [];
    const b = briefs[Number(args.key) - 1];
    if (!b)
      return { error: `No brief number ${args.key}; \`list${args.all ? " --all" : ""}\` shows ${briefs.length}.` };
    return { key: b.brief_key };
  }
  if (args.session) return { fromSession: args.session };
  return { key: autoBriefKey(scope.project, branch) };
}

async function transcriptBlock(post, sessions, turns) {
  const out = [];
  for (const sid of sessions) {
    const local = localTranscript(sid);
    const { found, messages, total, capped } = await recordedTurns(post, sid, turns);
    if (found && messages.length) out.push(renderTranscript(sid, messages, { total, capped }));
    else
      out.push(
        `## Session ${sid} — no recorded turns in Loom (recording was off for its namespace, or it was not a coding session).`,
      );
    if (local) out.push(`Exact, lossless restore on this machine: \`claude --resume ${sid}\` (transcript ${local}).`);
  }
  return out.join("\n\n");
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const token = resolveToken();
  if (!token) {
    process.stdout.write("No Loom token found (run the loom-status skill).\n");
    return;
  }
  const scope = resolveScope(process.cwd());
  const base = { org: scope.org, namespace: scope.namespace, agent: scope.agent };
  const post = (op, body) =>
    postBounded(op, { ...base, ...body }, { token, serverUrl: resolveServerUrl(), timeoutMs: TIMEOUT_MS });
  const branch = git(["rev-parse", "--abbrev-ref", "HEAD"]);

  if (args.cmd === "list") {
    const r = await post("session.brief_list", { project: args.all ? "" : scope.project, limit: LIST_LIMIT });
    if (!r) return void process.stdout.write("Loom did not answer (run the loom-status skill).\n");
    const briefs = Array.isArray(r.briefs) ? r.briefs : [];
    process.stdout.write(formatList(briefs, { scopeNote: args.all ? "" : ` for project ${scope.project}` }));
    return;
  }

  if (args.cmd === "save") {
    let sections;
    try {
      sections = JSON.parse(readFileSync(0, "utf8"));
    } catch (e) {
      process.stdout.write(`save reads the sections as JSON on stdin: ${e.message}\n`);
      process.exitCode = 2;
      return;
    }
    const r = await post("session.brief_save", {
      brief_key: args.key || autoBriefKey(scope.project, branch),
      project: scope.project,
      branch,
      head_sha: git(["rev-parse", "HEAD"]),
      repo: scope.repo ? scope.repo.slug : "",
      trigger: "model",
      sections,
    });
    if (!r) return void process.stdout.write("Loom did not save the brief (run the loom-status skill).\n");
    process.stdout.write(`Saved ${r.brief_key} revision ${r.revision}.\n\n${r.rendered || ""}\n`);
    return;
  }

  const sel = await resolveKey(args, post, scope, branch);
  if (sel.error) return void process.stdout.write(`${sel.error}\n`);
  const r = await post("session.brief_get", {
    brief_key: sel.key || "",
    from_session: sel.fromSession || "",
    revision: args.revision,
    // The brief and the client's compaction summary together recall more of a
    // session than either alone, so a restore always loads both.
    full: true,
  });
  if (!r) return void process.stdout.write("Loom did not answer (run the loom-status skill).\n");
  if (!r.found) {
    const what = sel.key || `from session ${sel.fromSession}`;
    const why = r.deleted ? " (it was deleted)" : "";
    process.stdout.write(`No session brief ${what}${why}. \`list --all\` shows the saved ones.\n`);
    return;
  }
  const head = String(r.head_sha || "");
  const known = head ? git(["rev-parse", "--verify", "--quiet", `${head}^{commit}`]) : "";
  const log = !head ? "" : known ? git(["log", "--oneline", `${head}..HEAD`]) : null;
  const since = sinceLines(head, { log, status: git(["status", "--short"], { raw: true }) });
  let out = `${r.rendered}\n${since.length ? `\n${since.join("\n")}\n` : ""}`;
  if (args.transcript) {
    const sessions = args.session ? [args.session] : (r.source_sessions || []).slice(-3);
    out += `\n# Full restoration: the recorded turns of ${sessions.length} session${sessions.length === 1 ? "" : "s"}\n\n`;
    out += `${await transcriptBlock(post, sessions, args.turns)}\n`;
  }
  process.stdout.write(out);
}

/** What moved in the checkout since the brief was saved: commits after its
 *  head (when that commit is known here) and the uncommitted changes. */
export function sinceLines(headSha, { log, status } = {}) {
  const lines = [];
  if (headSha) {
    const commits = (log || "").split("\n").filter(Boolean);
    if (log === null) lines.push(`The brief was saved at ${headSha.slice(0, 12)}, which this checkout does not have.`);
    else if (commits.length) {
      lines.push(
        `Since the brief was saved at ${headSha.slice(0, 12)}: ${commits.length} commit${commits.length === 1 ? "" : "s"}`,
      );
      for (const c of commits.slice(0, 8)) lines.push(`  ${c}`);
    } else lines.push(`No commits since the brief was saved at ${headSha.slice(0, 12)}.`);
  }
  const dirty = (status || "").split("\n").filter(Boolean);
  if (dirty.length) lines.push(`Uncommitted now: ${dirty.slice(0, 6).join(", ")}${dirty.length > 6 ? ", …" : ""}`);
  return lines;
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") {
  main().catch((e) => {
    process.stdout.write(`loom-brief failed: ${String((e && e.message) || e).slice(0, 200)}\n`);
    process.exitCode = 1;
  });
}
