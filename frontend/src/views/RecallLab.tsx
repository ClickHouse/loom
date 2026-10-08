import { useEffect, useState } from "react";
import { FlaskConical, Search, History } from "lucide-react";
import { memorySearchTrace, listSearchTraceLog, type SearchTraceHit, type SearchTraceLogRow } from "../api";
import { useStore, useIdentity } from "../lib/state";
import { PageContainer } from "../layout/PageContainer";
import { MemoryTabs } from "../components/MemoryTabs";
import { EmptyState } from "../components/EmptyState";
import { ScopeBadge } from "../components/ScopeBadge";
import { humanizeError, relativeTime, displayAgent } from "../lib/format";

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

// Recall Lab — the "why did this rank here" surface. It calls
// memory.search_trace, the single VECTOR leg of retrieval (cosine similarity +
// entity boost + blended rank_score), and lays the per-candidate breakdown out
// as ranked rows. Deliberately NOT the hybrid ranking the Memories list uses —
// it's the relevance lens for debugging recall, which is exactly what the
// server op was built for ("/me Recall Lab").
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
  const [hits, setHits] = useState<SearchTraceHit[] | null>(null);
  const [model, setModel] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Low-confidence result sets stay collapsed until the user opts in.
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
    setShowWeak(false); // each new query starts collapsed if weak
    // Once the fetch is in flight, clearing the timer no longer stops it: an answer that
    // lands after the query or namespace changed is dropped, not shown under the new query.
    let live = true;
    const t = setTimeout(async () => {
      setLoading(true);
      setError(null);
      try {
        const r = await memorySearchTrace(identity, q, { top_k: 25 });
        if (!live) return;
        setHits(r.results || []);
        setModel(r.embedding_model || "");
      } catch (e) {
        if (!live) return;
        setError(humanizeError(e));
        setHits([]);
      } finally {
        if (live) setLoading(false);
      }
    }, 250);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [query, identity]);

  // Best cosine similarity in the result set (hits are rank-ordered). Drives a
  // confidence hint: text-embedding-3-small has a high baseline, so even
  // unrelated queries return ~0.25-0.35 neighbours — a low top score means
  // "nearest vectors", not real matches.
  const topSim = hits && hits.length > 0 ? hits[0].similarity : 0;
  const NOISE_FLOOR = 0.3; // mirrors server memory_search_abstain_threshold
  const WEAK_MATCH = 0.5; // below this, matches are weak/semantically distant
  const lowConfidence = hits != null && hits.length > 0 && topSim < WEAK_MATCH;
  // Hide by default ONLY when the top match is below the noise floor (true
  // junk). Weak-but-plausible matches (e.g. a bare acronym like "COW" at ~0.4,
  // where the right memories are actually at the top) still show under the
  // banner — collapsing those hid legitimate results.
  const belowNoiseFloor = hits != null && hits.length > 0 && topSim < NOISE_FLOOR;
  const showResults = !belowNoiseFloor || showWeak;

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
            Why did a memory rank where it did? These scores are the <strong>vector leg</strong> of retrieval — cosine
            similarity, entity boost, and the blended rank score for each candidate. The Memories list runs the full
            hybrid pipeline, so its ordering can differ.
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
          {lowConfidence && (
            <div className="mb-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2 text-xs text-[var(--color-text-muted)]">
              {topSim < NOISE_FLOOR ? (
                <>
                  No strong match — the closest memory is only{" "}
                  <strong className="text-[var(--color-text)]">{(topSim * 100).toFixed(0)}%</strong> similar, below the
                  recall noise floor. These are the nearest vectors, not real matches.
                </>
              ) : (
                <>
                  Low-confidence matches — the closest is{" "}
                  <strong className="text-[var(--color-text)]">{(topSim * 100).toFixed(0)}%</strong> similar. Recall Lab
                  ranks by semantic (vector) similarity only; for exact keyword matches use the Memories search.
                </>
              )}
              {belowNoiseFloor && !showWeak && (
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
              {hits.map((h) => (
                <div
                  key={h.memory_key}
                  className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]"
                >
                  <div className="flex items-start gap-3">
                    <span className="shrink-0 mt-0.5 w-6 text-right text-xs font-mono text-[var(--color-text-subtle)]">
                      #{h.rank}
                    </span>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-[var(--color-text)] break-words">{h.content_excerpt}</p>
                      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs font-mono text-[var(--color-text-muted)]">
                        <span title="cosine similarity (1 − distance)">sim {h.similarity.toFixed(3)}</span>
                        {h.entity_boost > 0 && (
                          <span className="text-[var(--color-accent)]" title="entity-filter overlap boost">
                            entity +{h.entity_boost.toFixed(2)}
                          </span>
                        )}
                        {/* score = similarity + boosts; only show it when a boost
                          actually moved it, otherwise it just duplicates sim. */}
                        {Math.abs(h.rank_score - h.similarity) > 0.0005 && (
                          <span title="final blended score after boosts">score {h.rank_score.toFixed(3)}</span>
                        )}
                        <span className="px-1.5 py-0.5 rounded bg-[var(--color-surface-2)]">{h.source}</span>
                        <span className="text-[var(--color-text-subtle)]">{h.memory_type}</span>
                      </div>
                      {/* similarity bar — quick visual of relative relevance */}
                      <div className="mt-1.5 h-1 rounded-full bg-[var(--color-surface-2)] overflow-hidden">
                        <div
                          className="h-full bg-[var(--color-accent)]"
                          style={{ width: `${Math.max(0, Math.min(100, h.similarity * 100)).toFixed(1)}%` }}
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
