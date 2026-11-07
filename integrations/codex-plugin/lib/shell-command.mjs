// Shell-command shapes the code shunt judges: pure parsing, no I/O.
//
// A Bash command is judged only when its shape is certain: one file read
// whole or by a line range, a pattern search over named files or a tree, or
// a GitHub issue search. Anything the parser cannot follow (pipelines into
// a read, subshells, substitutions, heredocs, unknown flags with values)
// parses as nothing and passes. A false negative costs nothing; a false
// positive blocks a legitimate command.

const FLAGS_WITH_ARG = new Set([
  "-e",
  "-g",
  "-t",
  "-T",
  "-A",
  "-B",
  "-C",
  "-m",
  "-r",
  "--type",
  "--glob",
  "--regexp",
  "--max-count",
  "--context",
  "--after-context",
  "--before-context",
  "--include",
  "--exclude",
]);

function tokens(command) {
  const out = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = re.exec(command)) !== null) out.push(m[1] ?? m[2] ?? m[3]);
  return out;
}

/** A whole number > 0 or null. */
function lineNo(v) {
  const n = Number(v);
  return Number.isInteger(n) && n > 0 ? n : null;
}

/** gh search flags that take a value (the next word unless `--flag=value`). */
const GH_SEARCH_FLAGS_WITH_ARG = new Set([
  "--repo",
  "-R",
  "--limit",
  "-L",
  "--state",
  "-s",
  "--label",
  "-l",
  "--author",
  "-A",
  "--assignee",
  "-a",
  "--milestone",
  "-m",
  "--json",
  "-q",
  "--jq",
  "-t",
  "--template",
  "--sort",
  "--order",
  "--match",
  "--owner",
  "--language",
  "--created",
  "--updated",
  "--closed",
  "--comments",
  "--interactions",
  "--reactions",
  "--visibility",
  "--app",
  "--mention",
  "--involves",
  "--commenter",
  "--team-mentions",
  "--project",
  "--search",
  "-S",
]);

/** Shell words with backslash escapes honoured inside double quotes — a gh
 *  search phrase is often `--search "\\"exact phrase\\" word"`; `tokens()`
 *  (the grep shapes) has no escapes to honour. As in sh, a backslash there
 *  escapes only $ ` " \\ and keeps itself before anything else ("C:\\x"). */
function shTokens(command) {
  const out = [];
  const re = /"((?:\\.|[^"\\])*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = re.exec(command)) !== null) {
    out.push(m[1] != null ? m[1].replace(/\\([$`"\\])/g, "$1") : (m[2] ?? m[3]));
  }
  return out;
}

/** The issue-search shape of a `gh` command, or null.
 *  `gh issue list … --search|-S <q>` and `gh search issues <words…> [--repo …]`
 *  are searches of the repository's issue record; `gh issue view N`,
 *  `gh issue list` alone (a listing, not a question) and `gh pr …` are not.
 *  Returns {kind:"tracker_search", query, frames}; `frames` are the C++-qualified
 *  names the query carries (`DB::Foo::bar`), the strongest QA signal. */
export function ghIssueSearchShape(t) {
  if (!Array.isArray(t) || t[0] !== "gh") return null;
  let query = "";
  // The repository the command names (`--repo`/`-R`, or a
  // `repo:` qualifier) is the tracker it asks, not necessarily the checkout's.
  let repo = "";
  for (let i = 3; i < t.length; i++) {
    const tok = String(t[i] || "");
    if ((tok === "--repo" || tok === "-R") && t[i + 1]) repo = String(t[i + 1]);
    else if (tok.startsWith("--repo=")) repo = tok.slice("--repo=".length);
  }
  if (t[1] === "issue" && t[2] === "list") {
    for (let i = 3; i < t.length; i++) {
      const tok = t[i];
      if (tok === "--search" || tok === "-S") {
        query = String(t[i + 1] || "");
        break;
      }
      if (tok.startsWith("--search=")) {
        query = tok.slice("--search=".length);
        break;
      }
    }
  } else if (t[1] === "search" && t[2] === "issues") {
    const words = [];
    for (let i = 3; i < t.length; i++) {
      const tok = t[i];
      if (tok.startsWith("-")) {
        const base = tok.split("=")[0];
        if (GH_SEARCH_FLAGS_WITH_ARG.has(base) && !tok.includes("=")) i++;
        continue;
      }
      words.push(tok);
    }
    query = words.join(" ");
  }
  query = query.replace(/\s+/g, " ").trim();
  if (!query) return null;
  const qualified = /(?:^|\s)repo:([^\s]+)/i.exec(query);
  if (!repo && qualified) repo = qualified[1];
  // GitHub qualifiers (is:open, repo:x/y, in:title) are not defect words.
  const plain = query
    .split(" ")
    .filter((w) => !/^[a-z_-]+:[^\s]+$/i.test(w) || /::/.test(w))
    .join(" ")
    .trim();
  if (!plain) return null;
  const frames = Array.from(new Set(plain.match(/[A-Za-z_][\w]*(?:::[~A-Za-z_][\w]*)+/g) || []));
  return { kind: "tracker_search", query: plain.replace(/"/g, "").trim(), frames, repo: ghRepoSlug(repo) };
}

/** `owner/name`, lowercased, from a gh `--repo` value (`owner/name`,
 *  `github.com/owner/name`, a URL, `….git`); "" when it names none. */
export function ghRepoSlug(value) {
  const v = String(value || "")
    .trim()
    .replace(/\.git$/i, "")
    .replace(/\/+$/, "");
  const m = /(?:^|github\.com[/:])([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/.exec(v);
  return m ? `${m[1]}/${m[2]}`.toLowerCase() : "";
}

/** A command line that may be a `&&` / `;` chain: the first segment that is a
 *  gh issue search, or null. Pipelines and redirects still disqualify a
 *  segment (their output is not a question), quotes are honoured. */
export function ghIssueSearchInCommand(command) {
  const c = String(command || "");
  if (!c.trim()) return null;
  for (const seg of splitOutsideQuotes(c, LIST_SEPARATORS)) {
    const s = seg.trim();
    if (!s) continue;
    const bare = s.replace(/"(?:\\.|[^"\\])*"|'[^']*'/g, "");
    if (/[|>`$]/.test(bare)) continue;
    const shape = ghIssueSearchShape(shTokens(s));
    if (shape) return shape;
  }
  return null;
}

/** `awk '/re/…' files`, `sed -n '/re/p' file`, `git grep [-n] pattern files`
 *  as the grep shape parseBashBulk returns, or null when the command is not
 *  a pattern search (an awk NR range is a ranged read, judged elsewhere). */
export function patternSearchShape(cmd, t) {
  const positional = (from) => t.slice(from).filter((x) => !x.startsWith("-") && x !== "--");
  if (cmd === "awk") {
    const prog = t.slice(1).find((x) => !x.startsWith("-"));
    const m = prog && !/\bNR\b/.test(prog) ? /^\/((?:\\.|[^\/\\])+)\//.exec(prog) : null;
    if (!m) return null;
    const files = positional(1).slice(1);
    if (!files.length) return null;
    return { kind: "grep", pattern: m[1].replace(/\\(.)/g, "$1"), dir: files[0], single: true, files };
  }
  if (cmd === "sed") {
    const prog = t.slice(1).find((x) => !x.startsWith("-"));
    const m = prog ? /^\/((?:\\.|[^\/\\])+)\/p$/.exec(prog) : null;
    if (!m) return null;
    const files = positional(1).slice(1);
    if (!files.length) return null;
    return { kind: "grep", pattern: m[1].replace(/\\(.)/g, "$1"), dir: files[0], single: true, files };
  }
  if (cmd === "git" && t[1] === "grep") {
    const pos = positional(2);
    if (pos.length < 1) return null;
    const recursive = pos.length === 1 || t.slice(2).some((x) => /^-[A-Za-z]*r/.test(x) && !x.startsWith("--"));
    if (pos.length === 1) return { kind: "grep", pattern: pos[0], dir: "." };
    const files = pos.slice(1);
    return recursive
      ? { kind: "grep", pattern: pos[0], dir: files[0] }
      : { kind: "grep", pattern: pos[0], dir: files[0], single: true, files };
  }
  return null;
}

/** The span a shell read asks for: {kind:"read", file, offset, limit} — the
 *  same shape Read(offset, limit) carries, so classifyRead judges both alike
 *  (limit null = to the end of the file). null when the command is not a
 *  line-range read of one file (pattern-driven sed/awk, -c bytes, several
 *  files, unknown flags with values). */
export function parseRangedShellRead(cmd, argv) {
  const files = [];
  let offset = null;
  let limit = null;
  let program = null;
  let quiet = false;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (cmd === "sed") {
      if (a === "-n" || a === "--quiet" || a === "--silent") {
        quiet = true;
        continue;
      }
      if (a === "-e" || a === "--expression") {
        program = argv[++i];
        continue;
      }
      if (a.startsWith("-")) return null; // -i, -E, -s …: not a read we judge
      if (program == null) {
        program = a;
        continue;
      }
      files.push(a);
      continue;
    }
    if (cmd === "awk") {
      if (a === "-F" || a === "-v") {
        i++;
        continue;
      }
      if (a.startsWith("-")) return null;
      if (program == null) {
        program = a;
        continue;
      }
      files.push(a);
      continue;
    }
    // head / tail
    if (a === "-n" || a === "--lines") {
      program = argv[++i];
      continue;
    }
    if (a.startsWith("--lines=")) {
      program = a.slice(8);
      continue;
    }
    if (a === "-c" || a === "--bytes" || a.startsWith("--bytes=")) return null;
    if (/^-\+?\d+$/.test(a)) {
      program = a.slice(1);
      continue;
    } // head -50 / tail -+20
    if (a.startsWith("-")) continue; // -q, -v: harmless
    files.push(a);
  }
  if (files.length !== 1) return null;
  const file = files[0];
  if (cmd === "sed") {
    if (program == null) return null;
    if (!quiet) return { kind: "read", file }; // `sed 'A,Bp' f` prints the whole file (twice for the range)
    let m = /^(\d+),(\d+|\$)p$/.exec(program.trim());
    if (m) {
      offset = lineNo(m[1]);
      limit = m[2] === "$" ? null : Math.max(0, Number(m[2]) - Number(m[1]) + 1);
      return offset ? { kind: "read", file, offset, limit } : null;
    }
    m = /^(\d+)p$/.exec(program.trim());
    if (m && lineNo(m[1])) return { kind: "read", file, offset: lineNo(m[1]), limit: 1 };
    if (/^\$?p$/.test(program.trim()) || program.trim() === "p") return { kind: "read", file };
    return null; // /pattern/p, substitutions, scripts: not a range read
  }
  if (cmd === "awk") {
    if (program == null) return null;
    const pr = program.replace(/\s+/g, "");
    let m = /^NR>=(\d+)&&NR<=(\d+)$/.exec(pr) || /^NR==(\d+),NR==(\d+)$/.exec(pr);
    if (m) {
      offset = lineNo(m[1]);
      return offset ? { kind: "read", file, offset, limit: Math.max(0, Number(m[2]) - Number(m[1]) + 1) } : null;
    }
    if (/^(NR>=(\d+))$/.test(pr)) return { kind: "read", file, offset: lineNo(pr.slice(4)), limit: null };
    if (pr === "1" || pr === "{print}" || pr === "{print$0}") return { kind: "read", file };
    return null;
  }
  // head / tail
  if (cmd === "head") {
    const n = program == null ? 10 : lineNo(program);
    return n ? { kind: "read", file, offset: 1, limit: n } : null;
  }
  // tail
  if (program == null) return { kind: "read", file, offset: null, limit: 10 };
  if (program.startsWith("+")) {
    const from = lineNo(program.slice(1));
    return from ? { kind: "read", file, offset: from, limit: null } : null;
  }
  const n = lineNo(program);
  return n ? { kind: "read", file, offset: null, limit: n } : null;
}

/**
 * Parse a Bash command for the bulk-read shapes worth shunting:
 *   cat/bat/less/more <one file>            → {kind:"read", file}
 *   rg <pattern> [dir] / grep -r <pattern> [dir] → {kind:"grep", pattern, dir}
 *   sed -n 'A,Bp' <file> / head -n N <file> / tail -n +A <file> /
 *   awk 'NR>=A && NR<=B' <file> → {kind:"read", file, offset, limit}: a
 *   RANGED read, judged like Read(offset, limit): under the read-window cap
 *   when one is set, a pass otherwise. A shell slice is the same read.
 * Anything else → null (pass through): pipelines, redirects, pattern-driven
 * sed/awk, non-recursive grep, and commands with several files.
 * Deliberately conservative — a false negative costs nothing, a false
 * positive blocks a legitimate command.
 */
export function parseBashBulk(command) {
  const c = String(command || "").trim();
  if (!c) return null;
  // Shell metacharacters OUTSIDE quotes make this a pipeline, a redirect, a
  // list or an expansion — not a read we judge. Inside quotes they are the
  // program (`awk 'NR>=1 && NR<=9'`); a bare `,$p` is sed's "to the end".
  const bare = c.replace(/"[^"]*"|'[^']*'/g, "").replace(/,\$p\b/g, ",");
  if (/[|>;&`$]/.test(bare)) return null;
  const t = tokens(c);
  if (t.length < 2) return null;
  const cmd = t[0];
  const positional = [];
  let recursive = cmd === "rg";
  for (let i = 1; i < t.length; i++) {
    const tok = t[i];
    if (tok.startsWith("-")) {
      if (cmd === "grep" && /^-[A-Za-z]*[rR]/.test(tok) && !tok.startsWith("--")) recursive = true;
      if (cmd === "grep" && (tok === "--recursive" || tok === "--dereference-recursive")) recursive = true;
      const base = tok.split("=")[0];
      if (FLAGS_WITH_ARG.has(base) && !tok.includes("=") && !(cmd === "grep" && base === "-r")) i++;
      continue;
    }
    positional.push(tok);
  }
  if (["cat", "bat", "less", "more", "nl", "tac"].includes(cmd)) {
    return positional.length === 1 ? { kind: "read", file: positional[0] } : null;
  }
  // A pattern search spelled without grep is still a grep — `awk '/re/ …'
  // files`, `sed -n '/re/p' file`, `git grep pattern files` — e.g. awk to read
  // the checkout's line numbers after the grep was answered from the index.
  const asGrep = patternSearchShape(cmd, t);
  if (asGrep) return asGrep;
  if (["sed", "head", "tail", "awk"].includes(cmd)) return parseRangedShellRead(cmd, t.slice(1));
  if (cmd === "rg" || (cmd === "grep" && recursive)) {
    if (positional.length < 1 || positional.length > 2) return null;
    return { kind: "grep", pattern: positional[0], dir: positional[1] || "." };
  }
  // `grep [-n] pattern file...` (named files, not recursive)
  // is a single-file grep; classifyGrep passes it unless the pattern names a
  // symbol. Two files (the .cpp and its .h — the shape a model reaches for
  // when it wants a definition) are the same lookup:
  // `grep -n Sym MergeTreeData.cpp MergeTreeData.h` must not parse as nothing
  // and pass in silence.
  if (cmd === "grep" && positional.length >= 2) {
    return { kind: "grep", pattern: positional[0], dir: positional[1], single: true, files: positional.slice(1) };
  }
  return null;
}

/** A command's list separators: `;`, `&&`, `||` and newlines. */
const LIST_SEPARATORS = [";", "&&", "||", "\n"];

/** `text` cut at each of `separators` that sits outside quotes; quotes are
 *  kept in the pieces. Longer separators are tried first so `||` is not
 *  read as two pipes. */
export function splitOutsideQuotes(text, separators) {
  const seps = [...separators].sort((a, b) => b.length - a.length);
  const out = [];
  let cur = "",
    q = "";
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      cur += ch;
      if (ch === q) q = "";
      continue;
    }
    if (ch === '"' || ch === "'") {
      q = ch;
      cur += ch;
      continue;
    }
    const sep = seps.find((s) => text.startsWith(s, i));
    if (sep) {
      out.push(cur);
      cur = "";
      i += sep.length - 1;
      continue;
    }
    cur += ch;
  }
  out.push(cur);
  return out;
}

// Redirects that only discard or merge stderr/stdout change nothing about what
// a command reads: `grep -rn X server 2>/dev/null` is still the tree-wide grep.
const HARMLESS_REDIRECT = /(^|\s)(?:[12]?>\s*\/dev\/null|2>&1)(?=\s|$)/g;

/**
 * `parseBashBulk` over each segment of a compound command — the shape agents
 * actually send: `cd /repo; wc -l a.py; cat a.py`, `sed -n '1,80p' a.py; echo
 * ---; sed -n '90,120p' b.py`, `grep -rn sym server | head`. Returns the bulk shapes in command order,
 * each with `cmd` (its program) and `cwd` (the directory a preceding
 * `cd <literal>` moved to, relative to the hook's cwd or absolute; "" for
 * none).
 *
 * Pipelines: the first stage is judged when it is a search (a tree-wide grep
 * piped to `head` still ran the search); a read piped onward is skipped — the
 * next stage may slice it, so it is not a whole-file read. Still conservative:
 * a subshell, command substitution, heredoc or brace group anywhere makes the
 * whole command unjudged ([]), and a `cd` the parser cannot follow ends the
 * scan.
 */
/** On Windows, Git Bash's /c/x spelling of C:/x, which Node resolves as a
 *  path on the current drive; `p` as given elsewhere. */
export function gitBashPath(p, platform = process.platform) {
  const m = platform === "win32" ? /^\/([A-Za-z])(\/.*)?$/.exec(p) : null;
  return m ? `${m[1].toUpperCase()}:${m[2] || "/"}` : p;
}

export function parseBashBulkSegments(command) {
  const c = String(command || "");
  if (!c.trim()) return [];
  const bare = c.replace(/"[^"]*"|'[^']*'/g, "");
  if (/[`(){}]|<</.test(bare)) return [];
  const out = [];
  let cwd = "";
  for (const seg of splitOutsideQuotes(c, LIST_SEPARATORS)) {
    const s = seg.replace(HARMLESS_REDIRECT, "$1").trim();
    if (!s) continue;
    const words = tokens(s);
    if (words[0] === "cd") {
      const dir = words.length === 2 ? words[1] : "";
      // `~` expands only at the start of a word: C:/Users/RUNNER~1 is literal.
      if (!dir || dir.includes("$") || dir.startsWith("~") || dir === "-") break;
      const abs = gitBashPath(dir);
      cwd = /^(\/|[A-Za-z]:[\\/])/.test(abs) ? abs : cwd ? `${cwd}/${abs}` : abs;
      continue;
    }
    const stages = splitOutsideQuotes(s, ["|"]);
    const bulk = parseBashBulk(stages[0]);
    if (!bulk || (stages.length > 1 && bulk.kind !== "grep")) continue;
    out.push({ ...bulk, cmd: tokens(stages[0].trim())[0], cwd });
  }
  return out;
}
