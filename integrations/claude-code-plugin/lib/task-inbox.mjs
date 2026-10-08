// Pure helpers for the session-start task inbox line (the
// wake-up layer; unit-tested, no I/O).
//
// The task inbox plane is pull-based: an agent learns about work by calling
// task.pull, and nothing pushes a task into a session. This is the cheapest
// wake-up hint the plugin can give — ONE line of additionalContext at
// SessionStart when the caller's namespace owns inboxes with tasks waiting,
// naming the counts, the inbox, the oldest item's age and the op to call.
// Counts come from one `task.counts` call, cached for a minute per scope so
// two sessions opened together make one request. Silent on everything else:
// no token, an op the deployment does not serve (404), a token without the read (403), a slow
// server, an empty board.

export const CACHE_TTL_MS = 60 * 1000;
const WAITING = ["triage", "todo"];

/** Body for task.counts: the caller's own scope; the server filters the
 *  inboxes whose owner namespace is this one. */
export function countsBody(scope) {
  return { org: scope.org, namespace: scope.namespace, agent: scope.agent || "-" };
}

/** Roll the per-(inbox, status) rows up to what the line needs: how many
 *  wait in triage and todo, which inbox holds most of them, how many inboxes
 *  hold any, and the oldest waiting item's age in ms (null when unknown). */
export function summarizeCounts(result, now = Date.now()) {
  const rows = result && Array.isArray(result.counts) ? result.counts : [];
  const perInbox = new Map();
  let triage = 0;
  let todo = 0;
  let oldestMs = null;
  for (const r of rows) {
    if (!r || !WAITING.includes(String(r.status))) continue;
    const n = Number(r.n) || 0;
    if (n <= 0) continue;
    if (r.status === "triage") triage += n;
    else todo += n;
    const inbox = String(r.inbox || "");
    perInbox.set(inbox, (perInbox.get(inbox) || 0) + n);
    const t = r.oldest
      ? Date.parse(
          String(r.oldest)
            .replace(" ", "T")
            .replace(/(Z|[+-]\d\d:?\d\d)?$/, "Z"),
        )
      : NaN;
    if (Number.isFinite(t)) {
      const age = Math.max(0, now - t);
      oldestMs = oldestMs === null ? age : Math.max(oldestMs, age);
    }
  }
  const ranked = [...perInbox.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return {
    triage,
    todo,
    inbox: ranked.length ? ranked[0][0] : "",
    inboxes: ranked.length,
    oldestMs,
  };
}

/** "40 min" / "3 h" / "2 d"; "" when unknown. */
export function formatAge(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(Number(ms))) return "";
  const min = Math.round(Number(ms) / 60000);
  if (min < 60) return `${Math.max(1, min)} min`;
  const h = Math.round(min / 60);
  if (h < 48) return `${h} h`;
  return `${Math.round(h / 24)} d`;
}

/** The one line, or "" when nothing waits. */
export function renderTaskLine(summary) {
  if (!summary || summary.triage + summary.todo <= 0) return "";
  const parts = [];
  if (summary.triage > 0) parts.push(`${summary.triage} in triage`);
  if (summary.todo > 0) parts.push(`${summary.todo} to do`);
  const where = summary.inboxes > 1 ? `${summary.inbox} +${summary.inboxes - 1} more` : summary.inbox;
  const age = formatAge(summary.oldestMs);
  return (
    `Loom task inbox: ${parts.join(", ")} in ${where || "?"}` +
    `${age ? ` (oldest ${age})` : ""} — pull with the loom \`tasks\` toolset (task.pull)`
  );
}

/** The cached `{ ts, line }` entry is reusable while younger than the TTL. */
export function cacheIsFresh(cache, now = Date.now()) {
  return Boolean(cache && typeof cache.line === "string" && now - Number(cache.ts || 0) < CACHE_TTL_MS);
}
