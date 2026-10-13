import { useEffect, useState } from "react";
import { FlaskConical, Search, History } from "lucide-react";
import { searchMemory, listSearchTraceLog, type SearchHit, type SearchTraceLogRow } from "../api";
import { useStore, useIdentity } from "../lib/state";
import { PageContainer } from "../layout/PageContainer";
import { MemoryTabs } from "../components/MemoryTabs";
import { EmptyState } from "../components/EmptyState";
import { ScopeBadge } from "../components/ScopeBadge";
import { humanizeError, relativeTime, displayAgent } from "../lib/format";
import { signed, whyTerms } from "../lib/whyNote";

// One recorded search, expanded in place. A person reading "my agent answered badly an
// hour ago" needs more than the top score and the first eight keys — they need every key
// WITH its score (the two arrays are parallel on the wire), the whole query as it was
// sent, and which op sent it — a memory.ask query is what someone typed, a memory.search
// query from a harness may be text it composed.
const KEYS_FOLDED = 8;
const QUERY_FOLD_CHARS = 280;

/** Keys paired with their scores. A row written before scores were recorded
 *  (or a truncated projection) has fewer scores than keys; those keys show
 *  without a number rather than borrowing a neighbour's. */
export function pairKeysWithScores(keys: string[], scores: number[]): { key: string; score: number | null }[] {
  return keys.map((key, i) => ({ key, score: i < scores.length ? scores[i] : null }));
}

function RecallHistoryRow({ r }: { r: SearchTraceLogRow }) {
  const [allKeys, setAllKeys] = useState(false);
  const [fullQuery, setFullQuery] = useState(false);
  const pairs = pairKeysWithScores(r.returned_keys, r.returned_scores);
  const shown = allKeys ? pairs : pairs.slice(0, KEYS_FOLDED);
  // Bars are relative to the row's own best score, so a weak result set still
  // shows its shape instead of eight slivers.
  const top = pairs.reduce((m, p) => Math.max(m, p.score ?? 0), 0);
  const longQuery = r.query.length > QUERY_FOLD_CHARS;
  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0 text-sm text-[var(--color-text)]">
          {r.query !== "" ? (
            <>
              <div className={`break-words whitespace-pre-wrap ${longQuery && !fullQuery ? "line-clamp-3" : ""}`}>
                {r.query}
              </div>
              {longQuery && (
                <button
                  type="button"
                  onClick={() => setFullQuery((v) => !v)}
                  className="mt-0.5 text-2xs text-[var(--color-accent)] hover:underline"
                >
                  {fullQuery ? "Show less" : `Show full query (${r.query.length.toLocaleString()} chars)`}
                </button>
              )}
            </>
          ) : (
            <span className="italic text-[var(--color-text-subtle)]">wording not recorded</span>
          )}
        </div>
        <div className="text-2xs text-[var(--color-text-subtle)] whitespace-nowrap">{relativeTime(r.ts)}</div>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-[var(--color-text-muted)]">
        <span>
          <strong className="text-[var(--color-text)]">{r.result_count}</strong> of {r.top_k} returned
        </span>
        <span>{r.latency_ms} ms</span>
        {/* retrieval_risk is Literal["low","medium","high","abstain"],
            so the "abstain" band and abstain=true are the same fact.
            Showing both read as two separate problems ("abstained ·
            risk abstain"). One chip: the abstain outcome if it
            happened, otherwise the band when it is worth flagging. */}
        {r.abstain ? (
          <span
            className="text-[var(--color-warn,var(--color-text))]"
            title="Nothing passed the relevance floor, so loom returned no answer rather than a weak guess."
          >
            abstained
          </span>
        ) : (
          (r.retrieval_risk === "medium" || r.retrieval_risk === "high") && (
            <span title="Results were returned, but confidence was below the comfortable band.">
              {r.retrieval_risk} risk
            </span>
          )
        )}
        {r.operation !== "" && (
          <span
            className="px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] font-mono"
            title="The operation that ran this search. memory.ask and the MCP search tool carry the wording a person gave; a direct memory.search carries whatever the calling program composed."
          >
            via {r.operation}
          </span>
        )}
        <span>{displayAgent(r.agent)}</span>
        {r.run_id !== "" && r.run_id !== "-" && <span>run {r.run_id}</span>}
      </div>
      {pairs.length > 0 && (
        <ul className="mt-2 space-y-0.5" aria-label="Returned memories with scores">
          {shown.map(({ key, score }) => (
            <li key={key} className="grid grid-cols-[minmax(0,1fr)_3.5rem] items-center gap-x-2 text-2xs">
              <span className="min-w-0 truncate font-mono text-[var(--color-text-muted)]" title={key}>
                {key}
              </span>
              <span
                className="text-right font-mono tabular-nums text-[var(--color-text)]"
                title="blended rank score at the time of the search"
              >
                {score == null ? "–" : score.toFixed(3)}
              </span>
              <div
                className="col-span-2 h-0.5 rounded-full bg-[var(--color-surface-2)] overflow-hidden"
                aria-hidden="true"
              >
                <div
                  className="h-full bg-[var(--color-accent)]"
                  style={{
                    width: `${score == null || top <= 0 ? 0 : Math.max(0, Math.min(100, (score / top) * 100)).toFixed(1)}%`,
                  }}
                />
              </div>
            </li>
          ))}
          {pairs.length > KEYS_FOLDED && (
            <li>
              <button
                type="button"
                onClick={() => setAllKeys((v) => !v)}
                className="mt-1 text-2xs text-[var(--color-accent)] hover:underline"
              >
                {allKeys ? "Show fewer" : `Show all ${pairs.length}`}
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

// Searches that already ran, newest first. The scoring panel above re-runs a
// query against memory as it is NOW; only this can answer "my agent answered
// badly an hour ago — what did it actually retrieve then", because the memory
// may have changed since. Read-only, VIEWER, namespace-scoped server-side, so a
// non-operator sees their own history without an admin doing anything.
function RecallHistory() {
  const identity = useIdentity();
  // A run's replay opens this tab filtered to that run's recorded searches; the chip
  // clears it. Any window, since a run may be older than the default seven days.
  const runFilter = useStore((s) => s.recallRunFilter);
  const setRunFilter = useStore((s) => s.setRecallRunFilter);
  const [rows, setRows] = useState<SearchTraceLogRow[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hours, setHours] = useState(168);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const r = await listSearchTraceLog(identity, {
          since_hours: runFilter ? 720 : hours,
          limit: 100,
          ...(runFilter ? { run_id: runFilter } : {}),
        });
        if (!cancelled) setRows(r.rows || []);
      } catch (e) {
        if (!cancelled) {
          setError(humanizeError(e));
          setRows([]);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [identity, hours, runFilter]);

  // Recording is an operator switch (LOOM_SEARCH_TRACE_LOG_ENABLED) and the
  // query wording needs the namespace's own consent on top, so "empty" has two
  // very different causes. Saying which one avoids reading a settings choice as
  // a broken page.
  const anyQueryText = rows != null && rows.some((r) => r.query !== "");

  return (
    <>
      <div className="flex items-center justify-between gap-3 mb-3">
        <p className="text-sm text-[var(--color-text-muted)] max-w-2xl">
          Searches that already happened — what came back, whether recall abstained, and how long it took.
        </p>
        <select
          aria-label="Time window"
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
          className="px-2 py-1.5 text-xs rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
        >
          <option value={24}>Last 24 hours</option>
          <option value={168}>Last 7 days</option>
          <option value={720}>Last 30 days</option>
        </select>
      </div>

      {runFilter && (
        <div className="mb-3 flex items-center gap-2 text-xs">
          <span className="text-[var(--color-text-muted)]">Searches under run</span>
          <button
            type="button"
            onClick={() => setRunFilter(null)}
            title="Show every recorded search again"
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)] font-mono"
          >
            {runFilter.length > 28 ? `${runFilter.slice(0, 28)}…` : runFilter}
            <span aria-hidden="true">×</span>
            <span className="sr-only">clear run filter</span>
          </button>
          <span className="text-[var(--color-text-subtle)]">last 30 days</span>
        </div>
      )}

      {loading && <div className="text-sm text-[var(--color-text-muted)] px-1">Loading…</div>}
      {error && <div className="text-sm text-[var(--color-danger)] px-1">{error}</div>}

      {rows != null && !loading && !error && rows.length === 0 && (
        <EmptyState
          icon={History}
          title={runFilter ? "No recorded searches under this run" : "No recorded searches"}
          description={
            runFilter
              ? "A search counts for a run when the caller passed run_id on it or sent the X-Loom-Run-Id header (the SDK's run scope and the plugin do); this run's searches, if any, carried neither."
              : "Either nothing has been searched in this window, or per-query recall telemetry is switched off for this deployment."
          }
        />
      )}

      {rows != null && rows.length > 0 && (
        <>
          {!anyQueryText && (
            <div className="mb-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2 text-xs text-[var(--color-text-muted)]">
              Search wording isn&apos;t being recorded, so these rows show what was retrieved but not what was asked.
              Turn it on for this namespace in its settings &mdash;{" "}
              <em>&ldquo;Record what I search and what I store&rdquo;</em>.
            </div>
          )}
          <div className="space-y-2">
            {rows.map((r, i) => (
              <RecallHistoryRow key={`${r.ts}-${i}`} r={r} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

// Recall Lab — the "why did this rank here" surface: the Memories list's own search
// (memory.search with `explain: true`), each hit with the terms that ranked it.
export function RecallLab() {
  const identity = useIdentity();
  // A run's "Recall history for this run" opens the lab with the run filter set. The
  // filter lives on the history tab, so the page has to land there (and move there when
  // the filter arrives while it is open); otherwise the reader sees the empty score box
  // and no sign of the run.
  const runFilter = useStore((s) => s.recallRunFilter);
  const [tab, setTab] = useState<"score" | "history">(runFilter ? "history" : "score");
  useEffect(() => {
    if (runFilter) setTab("history");
  }, [runFilter]);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [abstain, setAbstain] = useState(false);
  const [model, setModel] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A result set the search abstained on stays collapsed until the user opts in.
  const [showWeak, setShowWeak] = useState(false);

  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setHits(null);
      setError(null);
      // A query cleared mid-flight drops that answer below, so its "Scoring…" ends here.
      setLoading(false);
      return;
    }
    setShowWeak(false);
    // An answer that lands after the query or namespace changed is dropped, not shown
    // under the new query; the abort stops the request itself.
    const ac = new AbortController();
    const t = setTimeout(async () => {
      setLoading(true);
      setError(null);
      try {
        // The same search the Memories list runs, with every hit's ranking terms.
        const r = await searchMemory(identity, q, { top_k: 25, explain: true, legs: ["memory"], signal: ac.signal });
        if (ac.signal.aborted) return;
        setHits(r.results || []);
        setAbstain(Boolean(r.abstain));
        setModel(r.embedding_model || "");
      } catch (e) {
        if (ac.signal.aborted) return;
        setError(humanizeError(e));
        setHits([]);
      } finally {
        if (!ac.signal.aborted) setLoading(false);
      }
    }, 250);
    return () => {
      ac.abort();
      clearTimeout(t);
    };
  }, [query, identity]);

  const showResults = !abstain || showWeak;
  const topScore = hits && hits.length > 0 ? hits[0].rank_score : 0;

  return (
    <PageContainer>
      <div className="mb-4">
        <h1 className="text-3xl font-semibold tracking-tight">Memories</h1>
        <div className="mt-1 mb-3">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </div>
        <MemoryTabs active="recall" />
        <h2 className="flex items-center gap-2 text-xl font-semibold text-[var(--color-text)]">
          <FlaskConical size={18} className="text-[var(--color-accent)]" /> Recall
        </h2>
        {tab === "score" && (
          <p className="mt-1 text-sm text-[var(--color-text-muted)] max-w-2xl">
            Why did a memory rank where it did? This runs the same search as the Memories list and shows each hit's
            similarity, final score and the ranking terms that moved it (key match, entity, recency, …).
          </p>
        )}
      </div>

      <div className="flex items-center gap-1 mb-4 border-b border-[var(--color-border)]">
        {(
          [
            ["score", "Score a query", FlaskConical],
            ["history", "Recall history", History],
          ] as const
        ).map(([id, label, Icon]) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={`inline-flex items-center gap-1.5 px-3 py-2 text-sm -mb-px border-b-2 transition-colors ${
              tab === id
                ? "border-[var(--color-accent)] text-[var(--color-text)]"
                : "border-transparent text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
            }`}
          >
            <Icon size={14} /> {label}
          </button>
        ))}
      </div>

      {tab === "history" && <RecallHistory />}

      <div className={tab === "score" ? "relative mb-4" : "hidden"}>
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-subtle)]" />
        <input
          aria-label="Recall query"
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search your memories — see the scoring breakdown"
          className="w-full pl-9 pr-3 py-2.5 text-base rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)] transition-colors"
        />
      </div>

      {tab === "score" && loading && <div className="text-sm text-[var(--color-text-muted)] px-1">Scoring…</div>}
      {tab === "score" && error && <div className="text-sm text-[var(--color-danger)] px-1">{error}</div>}

      {tab === "score" && hits != null && !loading && !error && hits.length === 0 && (
        <EmptyState
          icon={FlaskConical}
          title="No candidates"
          description="Nothing in this namespace scored against that query."
        />
      )}

      {tab === "score" && hits != null && hits.length > 0 && (
        <>
          {abstain && (
            <div className="mb-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2 text-xs text-[var(--color-text-muted)]">
              No confident match — the search abstained on this query. These are the nearest memories, not real matches.
              {!showWeak && (
                <>
                  {" "}
                  <button
                    onClick={() => setShowWeak(true)}
                    className="underline underline-offset-2 hover:text-[var(--color-text)] transition-colors"
                  >
                    Show {hits.length} anyway
                  </button>
                </>
              )}
            </div>
          )}
          {model && showResults && (
            <div className="text-2xs text-[var(--color-text-subtle)] mb-2 px-1">
              {hits.length} candidate{hits.length === 1 ? "" : "s"} · embedder{" "}
              <span className="font-mono">{model}</span>
            </div>
          )}
          {showResults && (
            <div className="space-y-1.5">
              {hits.map((h, i) => (
                <div
                  key={h.memory_key}
                  className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]"
                >
                  <div className="flex items-start gap-3">
                    <span className="shrink-0 mt-0.5 w-6 text-right text-xs font-mono text-[var(--color-text-subtle)]">
                      #{i + 1}
                    </span>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-[var(--color-text)] break-words">{h.content_excerpt}</p>
                      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs font-mono text-[var(--color-text-muted)]">
                        <span title="cosine similarity (1 − distance)">sim {(1 - h.distance).toFixed(3)}</span>
                        <span title="final score the list is ordered by">score {h.rank_score.toFixed(3)}</span>
                        {whyTerms(h.score_components).map((t) => (
                          <span
                            key={t.label}
                            className={t.value < 0 ? "text-[var(--color-warn)]" : "text-[var(--color-accent)]"}
                          >
                            {t.label} {signed(t.value)}
                          </span>
                        ))}
                        {h.source && (
                          <span className="px-1.5 py-0.5 rounded bg-[var(--color-surface-2)]">{h.source}</span>
                        )}
                        <span className="text-[var(--color-text-subtle)]">{h.memory_type}</span>
                      </div>
                      {/* score bar, relative to the top hit */}
                      <div className="mt-1.5 h-1 rounded-full bg-[var(--color-surface-2)] overflow-hidden">
                        <div
                          className="h-full bg-[var(--color-accent)]"
                          style={{
                            width: `${topScore > 0 ? Math.max(0, Math.min(100, (h.rank_score / topScore) * 100)).toFixed(1) : 0}%`,
                          }}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </PageContainer>
  );
}
