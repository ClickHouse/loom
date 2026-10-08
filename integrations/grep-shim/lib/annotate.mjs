// loom grep shim — pure helpers (no I/O) so they are unit-testable.
//
// The contract: real grep's output is never altered except that a matching
// line may gain a suffix naming the function it sits in. Every line grep
// printed is printed, in order, with its exit code. If anything here fails,
// the caller prints grep's output untouched.

// Output shapes we must NOT touch — the line is not `path:line:text`.
const GREP_OPTOUT_LONG = new Set([
  "--files-with-matches",
  "--files-without-match",
  "--count",
  "--only-matching",
  "--null",
  "--null-data",
  "--byte-offset",
  "--quiet",
  "--silent",
  "--no-filename",
  "--json",
  "--files",
  "--vimgrep",
  "--no-line-number",
  "--type-list",
  "--version",
  "--help",
  "--heading",
  "--pretty",
]);
const GREP_OPTOUT_SHORT = new Set(["l", "L", "c", "o", "Z", "z", "b", "q", "h", "N", "0", "V", "p"]);
// Short options that take an argument (the rest of the cluster, or the next token).
const GREP_ARG_SHORT = new Set(["e", "f", "m", "A", "B", "C", "d", "D", "g", "t", "T", "j", "M", "r", "E"]);
// Take a value in rg only: grep's -T, -r and -E are flags (initial tab,
// recursive, extended regexp); rg's are --type-not, --replace, --encoding.
const RG_ONLY_ARG = new Set(["g", "t", "T", "j", "M", "r", "E"]);

/**
 * Should the shim try to annotate this invocation? False for any output
 * shape that is not "path:line:text" per hit (file lists, counts, -o, NUL
 * separators, quiet). Requires line numbers: -n (or rg's --column, which
 * implies it). rg numbers lines by default only on a terminal, and the shim
 * annotates only piped output. `tool` is "grep" or "rg".
 */
export function annotatable(argv, tool = "grep", { singleFile = false } = {}) {
  let lineNumbers = false;
  let optionsDone = false;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (optionsDone || a === "-" || !a.startsWith("-")) continue;
    if (a === "--") {
      optionsDone = true;
      continue;
    }
    if (a.startsWith("--")) {
      const name = a.split("=", 1)[0];
      if (name === "--no-filename" && singleFile) continue; // path comes from the operand
      if (GREP_OPTOUT_LONG.has(name)) return false;
      if (name === "--line-number" || (tool === "rg" && name === "--column")) lineNumbers = true;
      if (name === "--color" && a.endsWith("=always")) return false;
      if (name === "--colour" && a.endsWith("=always")) return false;
      continue;
    }
    // short cluster like -rn, -rni, -A3, -e PATTERN
    for (let k = 1; k < a.length; k++) {
      const ch = a[k];
      if (ch === "h" && singleFile) continue; // path comes from the operand
      if (GREP_OPTOUT_SHORT.has(ch)) return false;
      if (ch === "n") lineNumbers = true;
      const takesArg = GREP_ARG_SHORT.has(ch) && (tool === "rg" || !RG_ONLY_ARG.has(ch));
      if (takesArg) {
        if (k === a.length - 1) i++; // value is the next token
        break; // rest of the cluster is the value
      }
    }
  }
  return lineNumbers;
}

/**
 * The FILE operands of a grep/rg invocation: positionals after the pattern
 * (the first positional, unless the pattern came via -e/-f/--regexp/--file).
 * Option values (-e X, -A 3, --include=*.cpp) are never operands.
 */
export function fileOperands(argv, tool = "grep") {
  const positionals = [];
  let patternInline = false;
  let optionsDone = false;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (optionsDone || a === "-" || !a.startsWith("-") || a.length === 1) {
      positionals.push(a);
      continue;
    }
    if (a === "--") {
      optionsDone = true;
      continue;
    }
    if (a.startsWith("--")) {
      const [name, , value] = a.split(/(=)(.*)/s);
      if (name === "--regexp" || name === "--file") {
        patternInline = true;
        if (value === undefined) i++;
      } else if (LONG_WITH_VALUE.has(name) && value === undefined) i++;
      continue;
    }
    for (let k = 1; k < a.length; k++) {
      const ch = a[k];
      if (ch === "e" || ch === "f") patternInline = true;
      const takesArg = GREP_ARG_SHORT.has(ch) && (tool === "rg" || !RG_ONLY_ARG.has(ch));
      if (takesArg) {
        if (k === a.length - 1) i++;
        break;
      }
    }
  }
  return patternInline ? positionals : positionals.slice(1);
}

// Long options whose value is the next token when not given as --opt=value.
const LONG_WITH_VALUE = new Set([
  "--max-count",
  "--after-context",
  "--before-context",
  "--context",
  "--include",
  "--exclude",
  "--exclude-dir",
  "--include-dir",
  "--color",
  "--colour",
  "--binary-files",
  "--devices",
  "--directories",
  "--label",
  "--type",
  "--type-not",
  "--glob",
  "--iglob",
  "--threads",
  "--max-columns",
  "--max-depth",
  "--max-filesize",
  "--encoding",
  "--sort",
  "--sortr",
  "--pre",
  "--replace",
  "--field-context-separator",
  "--field-match-separator",
  "--path-separator",
]);

const HIT = /^(.+?):(\d+):/;
const HIT_NO_PATH = /^(\d+):/;

/**
 * Locate annotatable hits in grep's output lines. A hit is `path:line:…`
 * where `path` resolves to a real file inside the repo (`toRel` returns the
 * repo-relative path, or null); with `singlePath` (exactly one file operand,
 * so grep omitted the filename) a hit is `line:…` and the path is the
 * operand. Context lines (`path-line-…`, `line-…`) and separators (`--`) are
 * left alone. Returns [{idx, rel, line}].
 */
export function parseHits(lines, toRel, singlePath = null) {
  const hits = [];
  if (singlePath) {
    // one file operand: grep prints `line:text` — the path is the operand
    const rel = toRel(singlePath);
    if (!rel) return hits;
    for (let idx = 0; idx < lines.length; idx++) {
      const m = HIT_NO_PATH.exec(lines[idx]);
      if (m) hits.push({ idx, rel, line: Number(m[1]) });
    }
    return hits;
  }
  for (let idx = 0; idx < lines.length; idx++) {
    const m = HIT.exec(lines[idx]);
    if (!m) continue;
    const rel = toRel(m[1]);
    if (!rel) continue;
    hits.push({ idx, rel, line: Number(m[2]) });
  }
  return hits;
}

/** Distinct `rel:line` keys in first-seen order, chunked for code.enclosing. */
export function locationBatches(hits, batchSize = 200, maxBatches = 5) {
  const keys = [];
  const seen = new Set();
  for (const h of hits) {
    const k = `${h.rel}:${h.line}`;
    if (seen.has(k)) continue;
    seen.add(k);
    keys.push(k);
  }
  const batches = [];
  for (let i = 0; i < keys.length && batches.length < maxBatches; i += batchSize) {
    batches.push(keys.slice(i, i + batchSize));
  }
  return batches;
}

export const MARK = "   ⟵ ";

/**
 * The suffix each resolved hit line gains, by line index. `scopes` maps
 * `rel:line` -> qualified_name ("" = unresolved, no suffix).
 */
export function hitSuffixes(hits, scopes) {
  const out = new Map();
  for (const h of hits) {
    const q = scopes.get(`${h.rel}:${h.line}`);
    if (q) out.set(h.idx, `${MARK}${q}`);
  }
  return out;
}

/**
 * Append the enclosing function to each hit line that resolved. Returns the
 * new lines array and how many hits were annotated.
 */
export function annotate(lines, hits, scopes) {
  const suffixes = hitSuffixes(hits, scopes);
  return { lines: lines.map((l, i) => (suffixes.has(i) ? l + suffixes.get(i) : l)), annotated: suffixes.size };
}

/** Fold a code.enclosing response into the `rel:line` -> qualified_name map. */
export function foldResults(scopes, results) {
  for (const r of results || []) {
    if (!r || !r.qualified_name) continue;
    scopes.set(`${r.path}:${r.line}`, String(r.qualified_name));
  }
  return scopes;
}

// ---------------------------------------------------------------------------
// The graph evidence block. Strictly trailing: it is appended
// after everything grep printed (and after the per-line suffixes), so
// stripGraphBlock() gives back the annotated output byte for byte. It is
// evidence beside the hits, never a reorder of them.
// ---------------------------------------------------------------------------

export const GRAPH_HEADER = "[Related from loom dependency graph]";

/**
 * Render code.enclosing's `neighbours` entries as one line per enclosing
 * function: name, module, then callers / callees / tests (each already
 * capped at k by the server) and the lower-bound note when the graph is
 * known to be incomplete for it. "" when nothing relates, or when more
 * functions were resolved than `maxAnchors` — a tree-wide grep for a common
 * word is not a question about a handful of functions.
 */
export function renderGraphBlock(neighbours, { maxAnchors = 20, capped = false } = {}) {
  const list = (neighbours || []).filter((n) => n && n.qualified_name);
  if (!list.length || capped || list.length > maxAnchors) return "";
  const rows = [];
  const testsShown = new Set(); // the test map is per source file: name its tests once
  for (const n of list) {
    const parts = [];
    if (n.callers?.length) parts.push(`callers: ${n.callers.join(", ")}`);
    if (n.callees?.length) parts.push(`callees: ${n.callees.join(", ")}`);
    if (n.tests?.length && !testsShown.has(n.path)) {
      testsShown.add(n.path);
      parts.push(`tests: ${collapsePaths(n.tests)}`);
    }
    if (!parts.length) continue; // nothing to relate: not a line
    const floor = n.epistemic === "lower_bound" && n.causes?.length ? ` (lower bound: ${n.causes.join(", ")})` : "";
    rows.push(`  ${n.qualified_name}${n.module ? ` [${n.module}]` : ""} — ${parts.join("; ")}${floor}`);
  }
  return rows.length ? `${GRAPH_HEADER}\n${rows.join("\n")}\n` : "";
}

/**
 * Paths grouped by directory, `dir/{a, b}` for a shared one: five stateless
 * tests share `tests/queries/0_stateless/`, which is where the tokens went
 * in the first measured blocks. Order preserved by first appearance.
 */
export function collapsePaths(paths) {
  const groups = new Map();
  for (const p of paths) {
    const i = p.lastIndexOf("/");
    const dir = i < 0 ? "" : p.slice(0, i);
    const base = i < 0 ? p : p.slice(i + 1);
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(base);
  }
  const out = [];
  for (const [dir, bases] of groups) {
    if (!dir) out.push(...bases);
    else out.push(bases.length === 1 ? `${dir}/${bases[0]}` : `${dir}/{${bases.join(", ")}}`);
  }
  return out.join(", ");
}

/** Text without its trailing graph block (unchanged when there is none). */
export function stripGraphBlock(text) {
  const s = String(text);
  const marker = `\n${GRAPH_HEADER}\n`;
  if (s.startsWith(`${GRAPH_HEADER}\n`)) return "";
  const i = s.lastIndexOf(marker);
  return i < 0 ? s : s.slice(0, i + 1);
}
