import { useEffect, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { listMemories, searchMemory, listSearchTraceLog, type SearchHit, type SearchTraceLogRow } from "../api";
import { useStore, useIdentity } from "../lib/state";
import { PageContainer } from "../layout/PageContainer";
import { MemoryTabs } from "../components/MemoryTabs";
import { EmptyState } from "../components/EmptyState";
import { ScopeBadge } from "../components/ScopeBadge";
import { Chip, PageHeader, TabTrigger, TipTarget } from "../components/ui";
import { humanizeError, relativeTime, displayAgent, previewValue, SENSITIVE_LABEL } from "../lib/format";
import { KEYWORD_PAGE, keywordThenRanked, parseSearchQuery } from "../lib/memoriesSearch";
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
    <div className="rounded-xl border border-border bg-surface px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0 text-sm text-text">
          {r.query !== "" ? (
            <>
              <div className={`break-words whitespace-pre-wrap ${longQuery && !fullQuery ? "line-clamp-3" : ""}`}>
                {r.query}
              </div>
              {longQuery && (
                <Button type="empty" htmlType="button" onClick={() => setFullQuery((v) => !v)} className="mt-0.5">
                  {fullQuery ? "Show less" : `Show full query (${r.query.length.toLocaleString()} chars)`}
                </Button>
              )}
            </>
          ) : (
            <span className="italic text-text-muted">wording not recorded</span>
          )}
        </div>
        <div className="text-xs text-text-muted whitespace-nowrap">{relativeTime(r.ts)}</div>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-muted">
        <span>
          <strong className="text-text">{r.result_count}</strong> of {r.top_k} returned
        </span>
        <span>{r.latency_ms} ms</span>
        {/* retrieval_risk is Literal["low","medium","high","abstain"],
            so the "abstain" band and abstain=true are the same fact.
            Showing both read as two separate problems ("abstained ·
            risk abstain"). One chip: the abstain outcome if it
            happened, otherwise the band when it is worth flagging. */}
        {r.abstain ? (
          <TipTarget tip="Nothing passed the relevance floor, so loom returned no answer rather than a weak guess.">
            <Badge text="abstained" state="warning" size="sm" />
          </TipTarget>
        ) : (
          (r.retrieval_risk === "medium" || r.retrieval_risk === "high") && (
            <TipTarget tip="Results were returned, but confidence was below the comfortable band.">
              <Badge text={`${r.retrieval_risk} risk`} state="warning" size="sm" />
            </TipTarget>
          )
        )}
        {r.operation !== "" && (
          <TipTarget tip="The operation that ran this search. memory.ask and the MCP search tool carry the wording a person gave; a direct memory.search carries whatever the calling program composed.">
            <Badge text={`via ${r.operation}`} state="neutral" size="sm" />
          </TipTarget>
        )}
        <span>{displayAgent(r.agent)}</span>
        {r.run_id !== "" && r.run_id !== "-" && <span>run {r.run_id}</span>}
      </div>
      {pairs.length > 0 && (
        <div className="mt-2 flex justify-end text-xs text-text-muted">
          <TipTarget tip="blended rank score at the time of the search" underline>
            score
          </TipTarget>
        </div>
      )}
      {pairs.length > 0 && (
        <ul className="mt-0.5 space-y-0.5" aria-label="Returned memories with scores">
          {shown.map(({ key, score }) => (
            <li key={key} className="grid grid-cols-[minmax(0,1fr)_3.5rem] items-center gap-x-2 text-xs">
              <span className="min-w-0 break-all font-mono text-text-muted">{key}</span>
              <span className="text-right font-mono tabular-nums text-text">
                {score == null ? "–" : score.toFixed(3)}
              </span>
              <div className="col-span-2 h-0.5 rounded-full bg-surface-2 overflow-hidden" aria-hidden="true">
                <div
                  className="h-full bg-accent"
                  style={{
                    width: `${score == null || top <= 0 ? 0 : Math.max(0, Math.min(100, (score / top) * 100)).toFixed(1)}%`,
                  }}
                />
              </div>
            </li>
          ))}
          {pairs.length > KEYS_FOLDED && (
            <li>
              <Button type="empty" htmlType="button" onClick={() => setAllKeys((v) => !v)} className="mt-1">
                {allKeys ? "Show fewer" : `Show all ${pairs.length}`}
              </Button>
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
        <p className="text-sm text-text-muted max-w-2xl">
          Searches that already happened — what came back, whether recall abstained, and how long it took.
        </p>
        <Select
          value={String(hours)}
          onSelect={(v) => setHours(Number(v))}
          triggerProps={{ "aria-label": "Time window" }}
          className="w-40 shrink-0"
        >
          <Select.Item value="24">Last 24 hours</Select.Item>
          <Select.Item value="168">Last 7 days</Select.Item>
          <Select.Item value="720">Last 30 days</Select.Item>
        </Select>
      </div>

      {runFilter && (
        <div className="mb-3 flex items-center gap-2 text-xs">
          <span className="text-text-muted">Searches under run</span>
          <Chip
            label={runFilter.length > 28 ? `${runFilter.slice(0, 28)}…` : runFilter}
            mono
            selected
            tip="Remove to show every recorded search again"
            onRemove={() => setRunFilter(null)}
          />
          <span className="text-text-muted">last 30 days</span>
        </div>
      )}

      {loading && <div className="text-sm text-text-muted px-1">Loading…</div>}
      {error && <div className="text-sm text-danger px-1">{error}</div>}

      {rows != null && !loading && !error && rows.length === 0 && (
        <EmptyState
          icon="history"
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
            <div className="mb-3 rounded-lg border border-border bg-surface-2 px-3 py-2 text-xs text-text-muted">
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

/** A ranked hit's similarity, final score and the terms that moved it. */
function RankedTerms({ hit }: { hit: SearchHit }) {
  return (
    <>
      <TipTarget tip="similarity the search reports for the hit">sim {hit.similarity.toFixed(3)}</TipTarget>
      <TipTarget tip="final score the ranked hits are ordered by">score {hit.rank_score.toFixed(3)}</TipTarget>
      {whyTerms(hit.score_components).map((t) => (
        <span key={t.label} className={t.value < 0 ? "text-warn" : "text-accent"}>
          {t.label} {signed(t.value)}
        </span>
      ))}
      {hit.source && <Badge text={hit.source} state="neutral" size="sm" />}
    </>
  );
}

/** One row of the Memories list's search order: a keyword match, a ranked hit, or both. */
interface RecallRow {
  key: string;
  text: string;
  memoryType: string;
  keyword: boolean;
  hit?: SearchHit;
}

// Recall Lab — the "why did this rank here" surface: the rows the Memories list shows for a
// search, in its order, each with what put it there.
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
  const [rows, setRows] = useState<RecallRow[] | null>(null);
  const [abstain, setAbstain] = useState(false);
  const [model, setModel] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The ranked search failed while keyword rows stood: those still show, with this said.
  const [rankedError, setRankedError] = useState<string | null>(null);
  // A result set the search abstained on stays collapsed until the user opts in.
  const [showWeak, setShowWeak] = useState(false);

  useEffect(() => {
    // The text the Memories list would send; its tag: / agent: filters are not applied here.
    const q = parseSearchQuery(query).text;
    // The Memories list waits for two characters; so does this.
    if (q.length < 2) {
      setRows(null);
      setError(null);
      setRankedError(null);
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
      setRankedError(null);
      try {
        // The two reads the Memories list makes for a search, with the same arguments.
        const search = searchMemory(identity, q, { top_k: 50, explain: true, legs: ["memory"], signal: ac.signal });
        search.catch(() => {}); // awaited below; a rejection before then is not unhandled
        const kw = await listMemories(identity, KEYWORD_PAGE, {
          contains: q,
          // `sensitivity` comes with every entry; naming it would 422 on an older replica
          fields: ["memory_key", "memory_type", "value"],
          previewChars: 200,
          signal: ac.signal,
        }).catch(() => null);
        // As on the Memories list, a failed ranked search keeps the keyword rows.
        const [r, failure] = await (kw?.entries.length
          ? search.then(
              (res) => [res, null] as const,
              (e: unknown) => [null, humanizeError(e)] as const,
            )
          : search.then((res) => [res, null] as const));
        if (ac.signal.aborted) return;
        const keyword = new Map((kw?.entries ?? []).map((e) => [e.memory_key, e] as const));
        const ranked = new Map((r?.results ?? []).map((h) => [h.memory_key, h] as const));
        const order = keywordThenRanked([...keyword.keys()], [...ranked.keys()]);
        setRows(
          order.map((key) => {
            const hit = ranked.get(key);
            const entry = keyword.get(key);
            return {
              key,
              // A high row comes without its value: say so rather than show an empty row.
              text:
                hit?.content_excerpt ?? (entry?.sensitivity === "high" ? SENSITIVE_LABEL : previewValue(entry?.value)),
              memoryType: hit?.memory_type ?? entry?.memory_type ?? "",
              keyword: entry !== undefined,
              hit,
            };
          }),
        );
        // Keyword matches are real matches: the list hedges only when there are none.
        setAbstain(Boolean(r?.abstain) && keyword.size === 0);
        setModel(r?.embedding_model ?? "");
        setRankedError(failure);
      } catch (e) {
        if (ac.signal.aborted) return;
        setError(humanizeError(e));
        setRows([]);
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
  const topScore = Math.max(0, ...(rows ?? []).map((row) => row.hit?.rank_score ?? 0));

  return (
    <PageContainer>
      <div className="mb-4">
        <PageHeader title="Memories" tab="Recall">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </PageHeader>
        <MemoryTabs active="recall" />
        <h2 className="flex items-center gap-2 text-text">
          <Icon name="flask" size="md" color="var(--color-accent)" aria-hidden="true" /> Recall
        </h2>
        {tab === "score" && (
          <p className="mt-1 text-sm text-text-muted max-w-2xl">
            Why did a memory land where it did on the Memories list? These are the rows it shows for a query with no
            filters set, in its order: memories whose text holds the query come first, newest first; then the ranked
            search, each hit with its similarity, final score and the terms that moved it (key match, entity, recency,
            …).
          </p>
        )}
      </div>

      <Tabs value={tab} onValueChange={(v) => setTab(v === "history" ? "history" : "score")}>
        <Tabs.TriggersList className="mb-4" aria-label="Recall">
          <TabTrigger value="score" icon="flask">
            Score a query
          </TabTrigger>
          <TabTrigger value="history" icon="history">
            Recall history
          </TabTrigger>
        </Tabs.TriggersList>
        <Tabs.Content value="history">
          <RecallHistory />
        </Tabs.Content>
        <Tabs.Content value="score">
          <div className="mb-4">
            <SearchField
              aria-label="Recall query"
              autoFocus
              value={query}
              onChange={(value) => setQuery(value)}
              placeholder="Search your memories — see the scoring breakdown"
            />
          </div>
          {loading && <div className="text-sm text-text-muted px-1">Scoring…</div>}
          {error && <div className="text-sm text-danger px-1">{error}</div>}
          {rankedError && !loading && (
            <div className="mb-2 text-xs text-text-muted px-1">
              Ranked search failed ({rankedError}); showing the keyword matches only.
            </div>
          )}

          {rows != null && !loading && !error && rows.length === 0 && (
            <EmptyState
              icon="flask"
              title="No candidates"
              description="Nothing in this namespace scored against that query."
            />
          )}

          {rows != null && rows.length > 0 && (
            <>
              {abstain && (
                <div className="mb-3 rounded-lg border border-border bg-surface-2 px-3 py-2 text-xs text-text-muted">
                  No confident match — the search abstained on this query. These are the nearest memories, not real
                  matches.
                  {!showWeak && (
                    <>
                      {" "}
                      <Button type="empty" htmlType="button" onClick={() => setShowWeak(true)}>
                        Show {rows.length} anyway
                      </Button>
                    </>
                  )}
                </div>
              )}
              {model && showResults && (
                <div className="text-xs text-text-muted mb-2 px-1">
                  {rows.length} row{rows.length === 1 ? "" : "s"} · embedder <span className="font-mono">{model}</span>
                </div>
              )}
              {showResults && (
                <div className="space-y-1.5">
                  {rows.map((row, i) => (
                    <div key={row.key} className="p-3.5 rounded-xl border border-border bg-surface">
                      <div className="flex items-start gap-3">
                        <span className="shrink-0 mt-0.5 w-6 text-right text-xs font-mono text-text-muted">
                          #{i + 1}
                        </span>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-mono text-text-muted break-all">{row.key}</p>
                          <p className="text-sm text-text break-words">{row.text}</p>
                          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs font-mono text-text-muted">
                            {row.keyword && (
                              <TipTarget tip="its text holds the query; the Memories list shows these first, newest first">
                                <Badge text="keyword match" state="neutral" size="sm" />
                              </TipTarget>
                            )}
                            {row.hit && <RankedTerms hit={row.hit} />}
                            <span className="text-text-muted">{row.memoryType}</span>
                          </div>
                          {row.hit && (
                            // score bar, relative to the top ranked hit
                            <div className="mt-1.5 h-1 rounded-full bg-surface-2 overflow-hidden">
                              <div
                                className="h-full bg-accent"
                                style={{
                                  width: `${topScore > 0 ? Math.max(0, Math.min(100, (row.hit.rank_score / topScore) * 100)).toFixed(1) : 0}%`,
                                }}
                              />
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </Tabs.Content>
      </Tabs>
    </PageContainer>
  );
}
