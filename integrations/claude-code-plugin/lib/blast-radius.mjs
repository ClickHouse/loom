// Pure helpers for the loom-blast-radius PostToolUse hook (unit-tested; no I/O).
//
// After Edit/Write/MultiEdit inside a loom-indexed repo, the hook diffs the
// edited file against HEAD, asks `code.impact` what the touched symbols reach,
// and hands the model ONE line: callers, tests that reach the change, and the
// files that historically change together but were not touched. This is the
// sibling-file failure — a fix lands in one file and misses the partner that
// always moves with it — surfaced at the moment of the edit rather than in
// review.
//
// Budget discipline: the line is ≤ ~90 tokens, emitted only when the radius
// is non-empty, and only when the set of touched symbols for that file
// changed since the last emission in this session (editing the same
// function five times costs one line, not five).

/** The code.impact body for an edit's diff outline. per_hop_limit keeps a hub's fan-out
 *  to one page (by_hop_counts still carries the true total), and the reach is all the
 *  hook renders, so it asks for no lessons. */
export function impactBody(repo, outline, { depth, perHop }) {
  return {
    org: repo.org,
    namespace: repo.namespace,
    diff: outline,
    depth,
    per_hop_limit: perHop,
    include_lessons: false,
    consumer: "claude-code-blast",
  };
}

/** Stable signature of what the change touches: sorted touched symbol names. */
export function blastSignature(rel, impact) {
  const names = (impact?.touched_symbols || [])
    .map((s) => String(s.qualified_name || ""))
    .filter(Boolean)
    .sort();
  return `${rel}|${names.join(",")}`;
}

/**
 * Session cache policy: emit when the signature for this file is new.
 * `cache` is {sig: {[rel]: signature}}; returns {emit, cache}.
 */
export function decideEmit(cache, rel, signature) {
  const sig = { ...((cache && cache.sig) || {}) };
  if (sig[rel] === signature) return { emit: false, cache: { sig } };
  sig[rel] = signature;
  const keys = Object.keys(sig);
  if (keys.length > 300) keys.slice(0, keys.length - 300).forEach((k) => delete sig[k]);
  return { emit: true, cache: { sig } };
}

function short(qname) {
  // DB::MergeTreeData::renameTempPartAndAdd -> MergeTreeData::renameTempPartAndAdd
  const parts = String(qname || "").split("::");
  return parts.length > 2 ? parts.slice(-2).join("::") : String(qname || "");
}

function base(p) {
  return String(p || "")
    .split("/")
    .pop();
}

/**
 * Render the blast line, or "" when there is nothing worth saying.
 * `touchedPaths` = files edited this session (repo-relative) so co-change
 * partners the agent already touched are not reported as missing.
 */
export function renderBlast(rel, impact, { maxNames = 5, touchedPaths = [] } = {}) {
  if (!impact) return "";
  const touched = (impact.touched_symbols || []).map((s) => short(s.qualified_name)).filter(Boolean);
  const impacted = (impact.impacted_symbols || []).filter((s) => s && s.qualified_name);
  const tests = (impact.impacted_tests || []).filter((t) => t && t.test_path);
  const already = new Set([rel, ...touchedPaths.map(String)]);
  const missing = (impact.co_change_missing || []).filter((m) => m && m.path && !already.has(String(m.path)));
  if (!impacted.length && !tests.length && !missing.length) return ""; // owners alone are not a radius

  const parts = [];
  let head = touched.length
    ? `Loom blast radius for ${rel} (edited ${touched.slice(0, 3).join(", ")}${touched.length > 3 ? ` +${touched.length - 3}` : ""})`
    : `Loom blast radius for ${rel}`;

  // by_hop_counts carries the TRUE hop-1 total; the rows may be a page of it
  // (the hook asks for per_hop_limit rows, the counts are unsliced).
  const hop1Total = Number((impact.by_hop_counts || {})[1] ?? (impact.by_hop_counts || {})["1"] ?? impacted.length);
  if (impacted.length) {
    const files = new Set(impacted.map((s) => String(s.path || "")));
    const names = [];
    const seen = new Set();
    for (const s of impacted) {
      const n = short(s.qualified_name);
      if (seen.has(n)) continue;
      seen.add(n);
      names.push(`${n} (${base(s.path)})`);
      if (names.length >= maxNames) break;
    }
    const total = Math.max(hop1Total, impacted.length);
    const more = total - names.length;
    parts.push(
      `${total} caller${total === 1 ? "" : "s"} in ${files.size}${impact.truncated ? "+" : ""} file${files.size === 1 && !impact.truncated ? "" : "s"}: ${names.join(", ")}${more > 0 ? ` +${more} more` : ""}`,
    );
  }
  if (tests.length) {
    const tp = [...new Set(tests.map((t) => String(t.test_path)))];
    parts.push(`tests reaching it: ${tp.length} (${tp.slice(0, 2).map(base).join(", ")}${tp.length > 2 ? ", …" : ""})`);
  }
  if (missing.length) {
    const m = missing.slice(0, 3).map((x) => `${x.path} (${x.prs} PRs)`);
    parts.push(
      `usually changes with, not touched yet: ${m.join(", ")}${missing.length > 3 ? ` +${missing.length - 3}` : ""}`,
    );
  }
  const reverts = (impact.reverts || []).filter((r) => r && r.pr_number);
  if (reverts.length) {
    parts.push(
      `reverted before: ${reverts
        .slice(0, 2)
        .map((r) => `#${r.pr_number}`)
        .join(", ")}${reverts.length > 2 ? ` +${reverts.length - 2}` : ""} — check why before repeating it`,
    );
  }
  const owners = (impact.owners || []).filter((o) => o && o.author);
  if (owners.length) {
    parts.push(`who knows this code: ${owners.map((o) => `${o.author} (${o.prs} PRs)`).join(", ")}`);
  }
  return `${head}: ${parts.join(" · ")}. ${epistemicNote(impact)}`;
}

/** The closing clause: exact for compiler-indexed static calls, else a floor with its causes. */
function epistemicNote(impact) {
  if (String(impact.epistemic || "") === "exact") return "Counts are exact for static calls (compiler-indexed).";
  const causes = (impact.causes || []).map(String).filter(Boolean);
  return causes.length ? `Counts are floors (${causes.join(", ")}).` : "Counts are floors (indexed code only).";
}

/** The hook's stdout payload: additional context only — never a decision. */
export function blastPayload(text) {
  return {
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext: String(text || ""),
    },
  };
}
