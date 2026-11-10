import { useCallback, useEffect, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
import { Table, type TableRowType } from "@clickhouse/click-ui/Table";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { listMemories, searchMemory, listSearchTraceLog, type SearchHit, type SearchTraceLogRow } from "../api";
import { useStore, useIdentity } from "../lib/state";
import { PageContainer } from "../layout/PageContainer";
import { MemoryTabs } from "../components/MemoryTabs";
import { EmptyState } from "../components/EmptyState";
import { ScopeBadge } from "../components/ScopeBadge";
import { Chip, ListTime, Loading, PageHeader, TabTrigger, TipTarget } from "../components/ui";
import { ListState } from "../components/ui/ListState";
import { ListToolbar } from "../components/ui/ListToolbar";
import { displayAgent, previewValue, SENSITIVE_LABEL } from "../lib/format";
import { humanizeError } from "../lib/errorWords";
import { KEYWORD_PAGE, keywordThenRanked, parseSearchQuery } from "../lib/memoriesSearch";
import { signed, termText, whyTerms } from "../lib/whyNote";

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
        <ListTime time={r.ts} className="text-xs text-text-muted" />
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
  const [error, setError] = useState<unknown>(null);
  const [hours, setHours] = useState(168);
  const [retry, setRetry] = useState(0);

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
          setError(e);
          setRows([]);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [identity, hours, runFilter, retry]);

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

      {loading && <Loading layout="inline" label="Loading recorded searches" className="px-1 mb-2" />}
      {error !== null && (
        <ListState kind="error" things="recorded searches" error={error} onRetry={() => setRetry((n) => n + 1)} />
      )}

      {rows != null && !loading && error === null && rows.length === 0 && (
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
          <div className="space-y-2" aria-busy={loading || undefined}>
            {rows.map((r, i) => (
              <RecallHistoryRow key={`${r.ts}-${i}`} r={r} />
            ))}
          </div>
        </>
      )}
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

// The entity boost has its own column; the other terms that moved a hit sit under its score.
const ENTITY = "entity";

const NUMERIC = "block w-full text-right [@media(max-width:768px)]:text-left tabular-nums";
// Under 769 px Click UI's Table lays each row out as a card of wrapping cells: the key and
// the value take a whole line each, the figures pair up two to a line, and none may grow
// past the card.
const CARD = "[@media(max-width:768px)]:min-w-0 [@media(max-width:768px)]:basis-1/2";
const CARD_WIDE = "[@media(max-width:768px)]:min-w-0 [@media(max-width:768px)]:basis-full";
const SCORE_HEADERS = [
  { label: <span className={NUMERIC}>#</span>, width: "48px" },
  { label: "Key", width: "24%" },
  { label: "Value" },
  { label: <span className={NUMERIC}>Similarity</span>, width: "96px" },
  { label: "Score", width: "160px" },
  { label: <span className={NUMERIC}>Entity</span>, width: "80px" },
  { label: "Type", width: "140px" },
];

/** One scored row: the key opens the memory (a button, so the keyboard reaches it), the
 *  score draws Click UI's table bar against the top ranked hit. */
function scoreRow(row: RecallRow, i: number, top: number, open: (key: string) => void): TableRowType {
  const hit = row.hit;
  const terms = whyTerms(hit?.score_components);
  const entity = terms.find((t) => t.label === ENTITY);
  const width = hit && top > 0 ? Math.max(0, Math.min(100, (hit.rank_score / top) * 100)) : 0;
  return {
    id: row.key,
    onClick: () => open(row.key),
    className: "cursor-pointer",
    items: [
      { className: CARD, label: <span className={`${NUMERIC} text-text-muted`}>{i + 1}</span> },
      {
        className: CARD_WIDE,
        label: (
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                open(row.key);
              }}
              className="min-w-0 max-w-full line-clamp-1 break-all rounded text-left font-mono text-xs text-text"
            >
              {row.key}
            </button>
            {row.keyword && (
              <TipTarget tip="its text holds the query; the Memories list shows these first, newest first">
                <Badge text="keyword match" state="neutral" size="sm" />
              </TipTarget>
            )}
          </div>
        ),
      },
      {
        className: CARD_WIDE,
        label: (
          <span className="line-clamp-1 break-words text-sm" title={row.text}>
            {row.text}
          </span>
        ),
      },
      { className: CARD, label: <span className={NUMERIC}>{hit ? hit.similarity.toFixed(3) : "—"}</span> },
      {
        className: CARD,
        label: hit ? (
          <div className="min-w-0 w-full">
            <span className="tabular-nums">{hit.rank_score.toFixed(3)}</span>
            <span className="recall-bar" aria-hidden="true">
              <span style={{ width: `${Math.round(width * 10) / 10}%` }} />
            </span>
            {terms.some((t) => t !== entity) && (
              <span className="mt-0.5 flex flex-wrap gap-x-2 text-xs text-text-muted">
                {terms
                  .filter((t) => t !== entity)
                  .map((t) => (
                    <span key={t.label}>
                      {t.label} {termText(t)}
                    </span>
                  ))}
              </span>
            )}
          </div>
        ) : (
          "—"
        ),
      },
      { className: CARD, label: <span className={NUMERIC}>{entity ? signed(entity.value) : "—"}</span> },
      {
        className: CARD,
        label: (
          <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5 text-xs text-text-muted">
            <span>{row.memoryType}</span>
            {hit?.source && <Badge text={hit.source} state="neutral" size="sm" />}
          </span>
        ),
      },
    ],
  };
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
  // The failed read itself, so its request id shows beside the message.
  const [error, setError] = useState<unknown>(null);
  // Bumped by Retry: the same query is asked again.
  const [attempt, setAttempt] = useState(0);
  const inspect = useStore((s) => s.inspect);
  const open = useCallback((key: string) => inspect(key, { agent: "*" }), [inspect]);
  // The ranked search failed while keyword rows stood: those still show, with this said.
  const [rankedError, setRankedError] = useState<string | null>(null);
  // A result set the search abstained on stays collapsed until the user opts in.
  const [showWeak, setShowWeak] = useState(false);
  // Another namespace's rows are not this one's to open: they go during render, before a
  // click could open the old namespace's key under the new one.
  const namespaceKey = `${identity.org}/${identity.namespace}`;
  const [rowsFor, setRowsFor] = useState(namespaceKey);
  if (rowsFor !== namespaceKey) {
    setRowsFor(namespaceKey);
    setRows(null);
    setAbstain(false);
    setRankedError(null);
    setShowWeak(false);
    setError(null);
  }

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
        setError(e);
        setRows([]);
      } finally {
        if (!ac.signal.aborted) setLoading(false);
      }
    }, 250);
    return () => {
      ac.abort();
      clearTimeout(t);
    };
  }, [query, identity, attempt]);

  const showResults = !abstain || showWeak;
  const topScore = Math.max(0, ...(rows ?? []).map((row) => row.hit?.rank_score ?? 0));
  const count =
    rows != null && rows.length > 0 && showResults && error == null
      ? `${rows.length} row${rows.length === 1 ? "" : "s"}${model ? ` · ${model}` : ""}`
      : undefined;

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
          <ListToolbar label="Score a query" count={count}>
            <div className="min-w-0 flex-1">
              <SearchField
                aria-label="Recall query"
                autoFocus
                value={query}
                onChange={(value) => setQuery(value)}
                placeholder="Search your memories — see the scoring breakdown"
              />
            </div>
          </ListToolbar>
          {loading && <Loading layout="inline" label="Loading scores" className="px-1 mb-2" />}
          {error != null && !loading && (
            <ListState kind="error" things="scores" error={error} onRetry={() => setAttempt((n) => n + 1)} />
          )}
          {rankedError && !loading && (
            <div className="mb-2 text-xs text-text-muted px-1">
              Ranked search failed ({rankedError}); showing the keyword matches only.
            </div>
          )}

          {rows != null && !loading && error == null && rows.length === 0 && (
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
              {showResults && (
                <Table
                  aria-label="Recall scores"
                  aria-busy={loading || undefined}
                  size="sm"
                  headers={SCORE_HEADERS}
                  rows={rows.map((row, i) => scoreRow(row, i, topScore, open))}
                />
              )}
            </>
          )}
        </Tabs.Content>
      </Tabs>
    </PageContainer>
  );
}
