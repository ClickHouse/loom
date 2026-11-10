/**
 * Finding a passage inside the Library's documents (`knowledge.search`), each hit cited
 * as an agent would cite it: its document, the revision it was read from and where it
 * sits (lines, a key, pages or slides, a sheet's rows). A hit opens its document.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { type Identity, type KnowledgeHit, searchKnowledge } from "../api";
import { Alert } from "../components/ui/Alert";
import { BusyButton } from "../components/ui/BusyButton";
import { ListRow } from "../components/ui/ListRow";
import { truncate } from "../lib/format";
import { humanizeError } from "../lib/errorWords";
import { citation } from "../lib/locator";
import { useLastPageFocus } from "../lib/useLastPageFocus";
import { useRowNav } from "../lib/useRowNav";

// The passages a search lists first, and how many more each "Show more" adds.
const TOP_K = 10;
// The most knowledge.search lists (`MAX_SEARCH_TOP_K`, server/knowledge/domain/documents.py).
const MAX_TOP_K = 100;

// What a hit shows of its passage; the document holds the rest.
const EXCERPT_CHARS = 280;

function excerpt(text: string): string {
  return truncate(text.replace(/\s+/g, " ").trim(), EXCERPT_CHARS);
}

/** The status line: what the hits on screen are. While more may be behind them (a read
 *  that asked for more has not come back with none new), they are the best so far. */
function countText(hits: KnowledgeHit[] | null, done: boolean): string {
  if (hits === null) return "";
  if (hits.length === 0) return "No passage answers that.";
  if (!done) return hits.length === 1 ? "The best passage" : `The best ${hits.length} passages`;
  return `${hits.length} passage${hits.length === 1 ? "" : "s"}`;
}

/** A hit's name among the others: its document and its chunk. */
function hitKey(h: KnowledgeHit): string {
  return `${h.source_uri}#${h.chunk_idx}`;
}

/** The last path segment of a source URI, its document's short name. */
function shortName(uri: string): string {
  return (
    uri
      .replace(/[?#].*$/, "")
      .replace(/\/+$/, "")
      .split("/")
      .pop() || uri
  );
}

/** The panel's id, for the toggle in the Library's toolbar that opens it (`aria-controls`). */
export const PASSAGES_PANEL_ID = "library-passages";

/** The open panel below the Library's toolbar; its toggle is the toolbar's. */
export function LibraryPassages({
  identity,
  navEnabled,
  onHitsShown,
  onOpen,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  /** The arrows and j/k move through the hits (off while a dialog is open). */
  navEnabled: boolean;
  /** Told whether hits show, so the list below hands its row keys over to them. */
  onHitsShown: (shown: boolean) => void;
  /** Open the document a hit is from. */
  onOpen: (sourceUri: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null);
  // What the hits on screen answer: the query searched, how many were asked for, and
  // whether asking for more found none (the op keeps one hit per section, so a short
  // answer is no sign that nothing is left).
  const [asked, setAsked] = useState({ query: "", topK: TOP_K, done: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const searching = useRef<AbortController | null>(null);
  // A search still running when the panel goes (another place) is dropped.
  useEffect(() => () => searching.current?.abort(), []);
  const shown = !!hits?.length;
  const rowNav = useRowNav({ enabled: navEnabled && shown });
  const listEl = useRef<HTMLOListElement | null>(null);
  const fieldEl = useRef<HTMLInputElement | null>(null);
  // Opened from the toolbar, which stays at the top as the list scrolls: the field takes
  // the focus, which brings the panel into view wherever the list was.
  useEffect(() => {
    fieldEl.current?.focus();
  }, []);
  const nav = useCallback(
    (el: HTMLOListElement | null) => {
      rowNav(el);
      listEl.current = el;
    },
    [rowNav],
  );
  useEffect(() => {
    onHitsShown(shown);
    return () => onHitsShown(false);
  }, [shown, onHitsShown]);

  const search = async (q: string, topK: number) => {
    if (!q) return;
    searching.current?.abort();
    const ctrl = new AbortController();
    searching.current = ctrl;
    setBusy(true);
    setError(null);
    try {
      const found = await searchKnowledge(identity, q, topK, { signal: ctrl.signal });
      if (!ctrl.signal.aborted) {
        const before = new Set(q === asked.query ? (hits ?? []).map(hitKey) : []);
        const added = found.some((h) => !before.has(hitKey(h)));
        setHits(found);
        setAsked({ query: q, topK, done: topK > TOP_K && !added });
      }
    } catch (e) {
      // The hits it would have replaced stay (a failed Show more, say).
      if (!ctrl.signal.aborted) setError(humanizeError(e, { read: true }));
    } finally {
      if (!ctrl.signal.aborted) setBusy(false);
    }
  };

  // More may be behind any answer until asking for more adds none, up to the most the op
  // lists.
  const more = !!hits?.length && !asked.done && asked.topK < MAX_TOP_K;
  // The last Show more removes the button that held the focus: it goes to the first hit
  // that read added, else the last hit, else the field.
  const markShown = useLastPageFocus({
    loading: busy,
    more,
    rows: () =>
      Array.from(listEl.current?.querySelectorAll<HTMLElement>("[data-hit-key]") ?? [], (el) => [
        el.dataset.hitKey ?? "",
        el,
      ]),
    fallback: () => fieldEl.current,
  });
  const count = countText(hits, asked.done);

  // Built once per answer, not per keystroke in the field above it.
  const list = useMemo(
    () =>
      hits && (
        <ol ref={nav} className="space-y-2" aria-label="Passages found" aria-busy={busy || undefined}>
          {hits.map((h, i) => {
            const cited = citation(h);
            const name = shortName(h.source_uri);
            // A key the citation already names is not named again.
            const matched = (h.keys_matched ?? []).filter((k) => k !== h.locator?.key_path);
            const excerptId = `${PASSAGES_PANEL_ID}-excerpt-${i}`;
            return (
              <li key={hitKey(h)}>
                <ListRow>
                  <button
                    type="button"
                    data-row-open=""
                    data-hit-key={hitKey(h)}
                    // Named by what the row shows but the excerpt, which is its description.
                    aria-label={[
                      `Open ${h.source_uri}`,
                      h.section,
                      cited,
                      matched.length ? `matched ${matched.join(", ")}` : "",
                    ]
                      .filter(Boolean)
                      .join(", ")}
                    aria-describedby={excerptId}
                    onClick={() => onOpen(h.source_uri)}
                    className="list-row-open block w-full"
                  >
                    <span className="flex flex-wrap items-center gap-2 text-sm">
                      <span className="font-medium break-all">{name}</span>
                      {/* The whole address too, so two files of one name tell apart. */}
                      {name !== h.source_uri && (
                        <span className="min-w-0 truncate font-mono text-xs text-text-muted">{h.source_uri}</span>
                      )}
                      {h.section && <span className="text-text-muted">{h.section}</span>}
                      {cited && <Badge size="sm" state="neutral" text={cited} />}
                      {matched.map((k) => (
                        <Badge key={k} size="sm" state="neutral" text={k} />
                      ))}
                    </span>
                    <span id={excerptId} className="mt-1 line-clamp-2 text-sm text-text-muted">
                      {excerpt(h.content)}
                    </span>
                  </button>
                </ListRow>
              </li>
            );
          })}
        </ol>
      ),
    [hits, busy, nav, onOpen],
  );

  return (
    <section id={PASSAGES_PANEL_ID} aria-label="Find a passage" className="mb-4 rounded-sm border border-border p-3">
      <div className="space-y-3">
        <p id={`${PASSAGES_PANEL_ID}-hint`} className="text-xs text-text-muted">
          Searches every document in this library, whatever the tag filter. Each passage is cited by the revision it was
          read from and its place.
        </p>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void search(query.trim(), TOP_K);
          }}
        >
          <div className="min-w-0 flex-1">
            <SearchField
              ref={fieldEl}
              value={query}
              onChange={setQuery}
              placeholder="What the passage says or names, e.g. retention period"
              aria-label="Passage to find"
              aria-describedby={`${PASSAGES_PANEL_ID}-hint`}
            />
          </div>
          <BusyButton type="secondary" htmlType="submit" disabled={!query.trim()} busy={busy}>
            Find
          </BusyButton>
        </form>
        {error && <Alert role="alert" state="danger" text={error} />}
        {/* In the page from the start, so a screen reader announces each new count. */}
        <p role="status" className="text-xs text-text-muted">
          {count}
        </p>
        {shown && list}
        {more && hits && (
          <BusyButton
            type="secondary"
            htmlType="button"
            busy={busy}
            onClick={() => {
              markShown(hits.map(hitKey));
              void search(asked.query, Math.min(asked.topK + TOP_K, MAX_TOP_K));
            }}
          >
            Show more
          </BusyButton>
        )}
      </div>
    </section>
  );
}
