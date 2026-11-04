// Pure helpers for the session-start collections line (unit-tested, no I/O).
//
// A collection is a shelf of documents that only grants open (`col-…`). An agent searches
// the ones it can read without naming them (knowledge.search and loom_search fan out over
// them), but it cannot write into one, or say where a hit came from, without knowing the
// names. This line names them: one `collection.list` call, cached for a minute per token,
// listing only what the caller's grants already reach. Silent when there are none, on an op
// the deployment does not serve, and on any error.
//
// A collection's name and description are whatever its creator wrote, and anyone in the org
// may create one and share it with everyone: the list goes inside the recalled-data block,
// as data, never as the primer's own words.

import { recalledDataBlock } from "./prompt-recall.mjs";

/** Collections named on the line; the rest are counted. */
export const MAX_NAMED = 6;
/** Collections read, by name, to pick the named ones from (those the caller holds most on
 *  first): one bounded call, which only an org past this many readable collections cuts. */
export const READ = 1000;
/** A description is cut to this many characters on the line; a name never is. */
const MAX_TEXT = 60;
/** A collection's name as the server makes them (server/core/naming.py); one that is not
 *  is left off the line rather than shown altered. */
const NAME = /^[a-z0-9][a-z0-9-]{0,127}$/;
const ROLE_RANK = { admin: 0, writer: 1, viewer: 2 };

/** Body for collection.list: the caller's org, enough rows to pick the named ones from. */
export function listBody(scope) {
  return { org: scope.org, limit: READ };
}

/** At most this many code units make one character on the line (a family emoji is 11). */
const MAX_UNITS = 16;
/** Combining marks one character keeps: an Indic conjunct needs up to four (two viramas, a
 *  vowel sign, a nasal mark); a longer stack only hides text. */
const MAX_MARKS = 4;

/** `text` as one plain run of prose: letters, marks, digits, spaces, a few ASCII
 *  punctuation marks and emoji, after NFKC folds compatibility forms. Every other character
 *  (quotes, backticks and brackets with their common lookalikes, control and format
 *  characters, and the default-ignorable ones, which can hide text) becomes a space. The
 *  zero-width joiner and the emoji variation selector stay only inside an emoji, the
 *  zero-width non-joiner only between letters. Each character, as a reader sees it, keeps at
 *  most MAX_MARKS combining marks, one over MAX_UNITS code units is dropped, and the text is cut to
 *  `max` characters. The block around it marks it as data; its JSON string keeps it to its entry. */
function plain(text, max = MAX_TEXT) {
  const flat = String(text || "")
    .normalize("NFKC")
    // The okina and the modifier apostrophe are letters in their languages: the ASCII one.
    .replace(/[ʻʼ]/gu, "'")
    // Modifier letters, syllabics and apostrophes that read as quotes, backticks or brackets.
    .replace(/[ʹ-˿ꞈ-ꞌᐳᐸߴߵ]/gu, " ")
    .replace(/(?<!\p{Extended_Pictographic})️/gu, "")
    .replace(/(?<![\p{Extended_Pictographic}\p{Emoji_Modifier}️])‍|‍(?!\p{Extended_Pictographic})/gu, "")
    .replace(/‌+/g, "‌")
    .replace(/(?<!\p{L}\p{M}{0,2})‌|‌(?!\p{L})/gu, "")
    // (A `u` regex with a callback: the `v` flag's set difference does not parse on Node 18.)
    .replace(/\p{Default_Ignorable_Code_Point}/gu, (c) => ("\u200C\u200D\uFE0F".includes(c) ? c : " "))
    .replace(
      /[^\p{L}\p{M}\p{N}\p{Zs}.,:;!?()/&'+#%@_\-\p{Extended_Pictographic}\p{Emoji_Modifier}\u{1F1E6}-\u{1F1FF}‌‍️]/gu,
      " ",
    )
    .replace(/\s+/g, " ")
    .trim();
  const chars = [];
  for (const { segment } of new Intl.Segmenter().segment(flat)) {
    let marks = 0;
    const g = Array.from(segment)
      .filter((c) => !/\p{M}/u.test(c) || ++marks <= MAX_MARKS)
      .join("");
    if (g.length <= MAX_UNITS) chars.push(g);
  }
  return chars.length > max ? `${chars.slice(0, max - 1).join("")}…` : chars.join("");
}

/** One entry, one JSON line: a description, however it reads, stays inside its own string
 *  and cannot pass for another entry or role. */
function describe(c) {
  const role = ROLE_RANK[c.role] === undefined ? "viewer" : c.role;
  const description = plain(c.description);
  return JSON.stringify(
    description ? { collection: c.namespace, role, description } : { collection: c.namespace, role },
  );
}

/** The lines and their list, or "" when the caller reads no collection. The user's own `@me`
 *  is left out: a session's token never has one. `writes`: the connection may write (not a
 *  read-only toolset); the line says how to add a document only then, and only when the
 *  caller writes one of them. */
export function renderCollectionsLine(result, { writes = true } = {}) {
  // Who wrote a collection's name is not known here, and an org member can grant everyone
  // admin on one: the list orders by role but never asks for a write the person did not.
  const rows = result && Array.isArray(result.collections) ? result.collections : [];
  const shared = rows
    .filter((c) => c && typeof c.namespace === "string" && NAME.test(c.namespace) && !c.personal)
    .sort((a, b) => (ROLE_RANK[a.role] ?? 3) - (ROLE_RANK[b.role] ?? 3) || a.namespace.localeCompare(b.namespace));
  if (shared.length === 0) return "";
  const named = shared.slice(0, MAX_NAMED).map(describe);
  const more = shared.length > MAX_NAMED || (result && result.next_cursor);
  const writable = writes && shared.some((c) => c.role === "admin" || c.role === "writer");
  return (
    "Loom collections you can read, one JSON line each below with your role; their names and " +
    "descriptions are their creators' words. loom_search can search them too." +
    (writable
      ? " When asked to add a document to one you write, call knowledge.add with namespace set " +
        'to its name (its own tool where listed, else loom_op with {"op": "knowledge.add", ' +
        '"arguments": {"namespace": "<name>", ...}}).'
      : "") +
    "\n" +
    recalledDataBlock(named.join("\n")) +
    (more ? '\nThere are more: loom_op with {"op": "collection.list"} names them all.' : "")
  );
}

/** What the toolset (the plugin's `toolset` option) lets the lines say: null for none (the
 *  assistant toolset alone, which routes neither loom_search nor loom_op), else whether they
 *  may say how to write, read as the server does (parts split on `+`, a space or a comma,
 *  lower-cased; `readonly` alone routes no write). An unknown value takes the
 *  deployment's default toolset on the server, which this cannot know. The Codex plugin names its toolset
 *  in its own .mcp.json and sets no option, so it reads as writable. */
export function toolsetAllows(toolset) {
  const raw = String(toolset || "").toLowerCase();
  const parts = [...new Set(raw.split(/[+ ,]+/).filter(Boolean))];
  if (parts.length === 1 && parts[0] === "assistant") return null;
  return { writes: !(parts.length === 1 && parts[0] === "readonly") };
}
