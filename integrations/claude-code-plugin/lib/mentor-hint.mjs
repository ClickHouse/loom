// Pure helpers for the loom-mentor-hint PostToolUse hook (unit-tested; no I/O).
//
// A successful loom tool result may carry one `hint` (and
// its `hint_code`) — the store naming the plane this agent is not using,
// from the call it just made. The model reads the tool result as JSON and
// can skip a key; this hook lifts the hint out of the result and hands it
// back as additionalContext, once per code per session, so the advice is
// read as advice and not as a field. Fail-open: no hint, a non-loom tool, a
// malformed result, a code already shown — nothing is printed.

const MAX_PER_SESSION = 6;

/** True for the loom MCP server's tools, whatever the client's prefix. */
export function isLoomTool(toolName) {
  const n = String(toolName || "");
  return /^mcp__loom__/.test(n) || /^loom_[a-z]/.test(n) || /mcp__[a-z0-9_-]*loom[a-z0-9_-]*__/.test(n);
}

function parseMaybeJson(text) {
  if (typeof text !== "string") return null;
  const t = text.trim();
  if (!t.startsWith("{")) return null;
  try {
    return JSON.parse(t);
  } catch {
    return null;
  }
}

/** The (code, text) a tool response carries, or null. Accepts the MCP
 *  result envelope (`structuredContent`, `content[0].text` as JSON), a bare
 *  object, or a JSON string. */
export function extractHint(toolResponse) {
  const candidates = [];
  const r = toolResponse;
  if (r && typeof r === "object") {
    candidates.push(r);
    if (r.structuredContent && typeof r.structuredContent === "object") candidates.push(r.structuredContent);
    if (Array.isArray(r.content)) {
      for (const block of r.content) {
        if (block && typeof block.text === "string") {
          const parsed = parseMaybeJson(block.text);
          if (parsed) candidates.push(parsed);
        }
      }
    }
  } else if (typeof r === "string") {
    const parsed = parseMaybeJson(r);
    if (parsed) candidates.push(parsed);
  }
  for (const c of candidates) {
    const text = typeof c.hint === "string" ? c.hint.trim() : "";
    if (text) {
      const code = typeof c.hint_code === "string" && c.hint_code ? c.hint_code : "hint";
      return { code, text: text.slice(0, 400) };
    }
  }
  return null;
}

/** Show a code at most once per session, and at most MAX_PER_SESSION hints. */
export function decideShow(cache, code) {
  const shown = Array.isArray(cache && cache.shown) ? cache.shown : [];
  if (shown.includes(code) || shown.length >= MAX_PER_SESSION) return { show: false, cache: { shown } };
  return { show: true, cache: { shown: [...shown, code] } };
}

/** The one line the model reads. */
export function renderHint(hint, toolName) {
  const tool = String(toolName || "").replace(/^mcp__[a-z0-9_-]+?__/, "");
  return `Loom mentor (${hint.code}${tool ? `, after ${tool}` : ""}): ${hint.text} — /loom-mentor has the table; mentor.brief the evidence.`;
}

/** PostToolUse stdout payload: additional context only — never a decision. */
export function hintPayload(line) {
  return line ? { hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: String(line) } } : null;
}
