// Pure helpers for the loom-session-memory hook (unit-tested; no I/O).
//
// A Claude Code session ends and nothing had asked loom to remember it: the
// conversation hook appends turns to the conversation plane, which a
// namespace records only when its owner turned recording on (fail-closed),
// and the run ledger says the session ended, not what it learned. This hook hands the
// transcript's real turns to memory.set_from_messages(defer=true) once, at
// SessionEnd: the server extracts the facts, writes the session summary,
// judges the outcome and, later, distils the reflections. Memories, not the
// transcript — the transcript itself still lands only where recording is on.

const MAX_TURNS = 200; // last N real turns of the session
const MAX_MESSAGE_CHARS = 6000; // per turn
const MAX_TOTAL_CHARS = 120000; // the whole payload's message text
const MIN_TURNS = 2; // one prompt and one reply, or nothing to remember
// What the session's own Loom tool calls said and heard.
const ECHO_MAX_VALUES = 80; // strings handed to the extractor
const ECHO_MAX_CHARS = 8000; // per string (a context pack's procedures sit past 4K)
const ECHO_MAX_TOTAL = 80000; // all of them together
const ECHO_MIN_CHARS = 8; // shorter args ("alice", "5") say nothing
const LOOM_TOOL_RE = /(?:^|__)loom_[a-z_]+$/;
const KEY_FIELDS = new Set(["memory_key", "key", "label", "source_uri", "block_key"]);
// What the session ASKED is not something a tool persisted or served: a
// search query, a question, a grep pattern stay the user's own words (a fact
// restating one is judged by the narration rules, not as an echo).
const ASK_FIELDS = new Set([
  "query",
  "question",
  "pattern",
  "situation",
  "name",
  "symbol",
  "path",
  "paths",
  "locations",
  "text",
]);

/** Real {role, content, timestamp} turns from transcript JSONL objects:
 *  user prompts (string content, or an array when an image was pasted — it
 *  becomes a "[image: …]" placeholder; tool results are skipped unless a tool
 *  returned an image, which leaves one `tool` placeholder turn; system-injected
 *  turns start with "<"), assistant text blocks joined. Sidechains skipped. */
import { contentText, hookClient, toolImageTurn } from "./loom-client.mjs";
import { runIdFor } from "./run-hook.mjs";

export function transcriptTurns(objs) {
  const out = [];
  for (const o of objs || []) {
    if (!o || o.isSidechain) continue;
    const msg = o.message;
    if (!msg) continue;
    const ts = typeof o.timestamp === "string" ? o.timestamp : "";
    if (o.type === "user") {
      if (Array.isArray(msg.content) && msg.content.some((b) => b && b.type === "tool_result")) {
        const img = toolImageTurn(msg.content);
        if (img) out.push({ ...img, timestamp: ts });
        continue;
      }
      const content = contentText(msg.content).trim();
      if (!content || content.startsWith("<")) continue;
      out.push({ role: "user", content, timestamp: ts });
      continue;
    }
    if (o.type === "assistant" && Array.isArray(msg.content)) {
      const text = contentText(msg.content).trim();
      if (text) out.push({ role: "assistant", content: text, timestamp: ts });
    }
  }
  return out;
}

/** A result's structuredContent as parseable JSON strings,
 *  each under ECHO_MAX_CHARS — the whole object when it fits, else one piece
 *  per row of each top-level list (a cut JSON string parses to nothing on
 *  the server, and its rows would never be compared). [] when absent. */
export function structuredPieces(sc) {
  if (!sc || typeof sc !== "object") return [];
  const whole = JSON.stringify(sc);
  if (whole.length <= ECHO_MAX_CHARS) return [whole];
  const out = [];
  const rest = {};
  for (const [k, v] of Object.entries(sc)) {
    if (Array.isArray(v)) for (const row of v) out.push(JSON.stringify({ [k]: [row] }));
    else rest[k] = v;
  }
  const r = JSON.stringify(rest);
  if (r !== "{}") out.push(r);
  return out.filter((x) => x.length <= ECHO_MAX_CHARS);
}

/** The text the session's own Loom tool calls carried — every
 *  string argument of a `loom_*` tool_use (the value it wrote, the query it
 *  asked, the title it saved) and every result it got back — plus the memory
 *  keys those calls named. The extractor drops a fact that restates any of
 *  them: the tool already persisted or served that truth, and a re-extracted
 *  copy can only duplicate it or, after a later update in the same session,
 *  contradict it ("I prefer squash merges" written back after the key moved
 *  to rebase). Claude Code transcript shape; anything else yields nothing. */
export function sessionToolEchoes(objs) {
  const values = [];
  const keys = new Set();
  const loomIds = new Set();
  const push = (text) => {
    const t = String(text || "").trim();
    if (t.length >= ECHO_MIN_CHARS) values.push(t.slice(0, ECHO_MAX_CHARS));
  };
  const walkArgs = (v, depth = 0) => {
    if (depth > 4 || v == null) return;
    if (typeof v === "string") return push(v);
    if (Array.isArray(v)) return v.forEach((x) => walkArgs(x, depth + 1));
    if (typeof v === "object") {
      for (const [k, x] of Object.entries(v)) {
        if (KEY_FIELDS.has(k) && typeof x === "string" && x.trim()) keys.add(x.trim());
        if (depth === 0 && ASK_FIELDS.has(k)) continue;
        walkArgs(x, depth + 1);
      }
    }
  };
  for (const o of objs || []) {
    // A Codex rollout records each MCP call as one
    // `item_completed` event (`McpToolCall`: tool, arguments, result) — the
    // Claude Code tool_use/tool_result pair below never matches it.
    const item =
      o && o.type === "event_msg" && o.payload && o.payload.type === "item_completed" ? o.payload.item : null;
    if (item && item.type === "McpToolCall" && (item.server === "loom" || LOOM_TOOL_RE.test(String(item.tool || "")))) {
      walkArgs(item.arguments);
      const r = item.result;
      // A Codex result's text block can be a pointer
      // ("Result in structuredContent.") and the answer rides structuredContent
      // alone — read that, or the echo check compares facts against the pointer.
      const pieces = structuredPieces(r && typeof r === "object" ? r.structuredContent : null);
      const text = pieces.length
        ? pieces.join("\n")
        : typeof r === "string"
          ? r
          : r && Array.isArray(r.content)
            ? contentText(r.content)
            : "";
      if (pieces.length) pieces.forEach(push);
      else push(text);
      for (const m of String(text).matchAll(/"memory_key"\s*:\s*"([^"]{1,200})"/g)) {
        if (!m[1].startsWith("chunk:")) keys.add(m[1]);
      }
      continue;
    }
    const content = o && o.message && Array.isArray(o.message.content) ? o.message.content : [];
    for (const b of content) {
      if (!b || typeof b !== "object") continue;
      if (b.type === "tool_use" && LOOM_TOOL_RE.test(String(b.name || ""))) {
        loomIds.add(b.id);
        walkArgs(b.input);
      } else if (b.type === "tool_result" && loomIds.has(b.tool_use_id)) {
        const text = typeof b.content === "string" ? b.content : contentText(b.content);
        push(text);
        for (const m of String(text).matchAll(/"memory_key"\s*:\s*"([^"]{1,200})"/g)) {
          if (!m[1].startsWith("chunk:")) keys.add(m[1]);
        }
      }
    }
  }
  const out = [];
  let total = 0;
  for (const v of [...new Set(values)]) {
    if (out.length >= ECHO_MAX_VALUES || total + v.length > ECHO_MAX_TOTAL) break;
    out.push(v);
    total += v.length;
  }
  return { values: out, keys: [...keys].slice(0, 200) };
}

function clip(text, max) {
  const s = String(text || "");
  return s.length <= max ? s : s.slice(0, max - 14).trimEnd() + "\n…[truncated]";
}

/** Body for memory.set_from_messages at SessionEnd, or null when the session
 *  holds fewer than MIN_TURNS real turns or no user prompt. The tail of the
 *  session is kept (the latest turns carry the outcome), each turn and the
 *  whole payload bounded. `defer: true` so the hook returns as soon as the
 *  request is queued; the extraction worker does the model work. */
export function sessionMemoryBody(scope, payload, turns, opts = {}) {
  const maxTurns = opts.maxTurns || MAX_TURNS;
  const maxTotal = opts.maxTotalChars || MAX_TOTAL_CHARS;
  const sessionId = String(payload.session_id || "").trim();
  if (!sessionId) return null;
  let tail = (turns || []).slice(-maxTurns);
  if (!tail.some((t) => t.role === "user") || tail.length < MIN_TURNS) return null;
  // Bound the total from the newest turn backwards: drop the oldest turns
  // until the payload fits, never the latest.
  const clipped = tail.map((t) => ({ ...t, content: clip(t.content, opts.maxMessageChars || MAX_MESSAGE_CHARS) }));
  let total = clipped.reduce((n, t) => n + t.content.length, 0);
  let start = 0;
  while (total > maxTotal && clipped.length - start > MIN_TURNS) {
    total -= clipped[start].content.length;
    start += 1;
  }
  tail = clipped.slice(start);
  if (!tail.some((t) => t.role === "user")) return null;
  const messages = tail.map((t) => {
    const m = { role: t.role, content: t.content };
    if (t.timestamp) m.timestamp = t.timestamp;
    return m;
  });
  const endedAt = payload.ended_at || opts.now || new Date().toISOString();
  return {
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent,
    session_id: sessionId,
    run_id: runIdFor(sessionId),
    messages,
    extra_tags: [`source:${hookClient()}`, ...(scope.tags || [])],
    observation_date: String(endedAt).slice(0, 10),
    defer: true,
    // The keys this session's Loom tools served (the capture hook's stash);
    // the extractor judges recall echoes against them.
    // Plus the keys its Loom calls wrote or read, and the
    // text those calls carried (`session_values`) — restating it is an echo.
    ...recalledKeysField(opts.recalledKeys, opts.echoes && opts.echoes.keys),
    ...(opts.echoes && opts.echoes.values && opts.echoes.values.length ? { session_values: opts.echoes.values } : {}),
  };
}

function recalledKeysField(recalled, echoed) {
  const all = [...(Array.isArray(recalled) ? recalled : []), ...(Array.isArray(echoed) ? echoed : [])]
    .map(String)
    .filter(Boolean);
  return all.length ? { recalled_keys: [...new Set(all)].slice(0, 200) } : {};
}
