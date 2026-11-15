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
  // Two places can hold the same address.
  return `${h.namespace ?? ""}/${h.source_uri}#${h.chunk_idx}`;
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

/** What a search reads: this library, or every place (how many once the places are known,
 *  and how many it leaves out). */
function hint(everywhere: boolean, unknown: boolean, places: number, notSearched: number): string {
  if (!everywhere && notSearched === 0) return "Searches every document in this library, whatever the tag filter.";
  if (unknown) return "Searches every document in every place you read, whatever the tag filter.";
  const left = notSearched > 0 ? `; ${notSearched} other place${notSearched === 1 ? " is" : "s are"} not searched` : "";
  return `Searches every document in ${places} place${places === 1 ? "" : "s"}, whatever the tag filter${left}.`;
}

/** The panel's id, for the toggle in the Library's toolbar that opens it (`aria-controls`). */
export const PASSAGES_PANEL_ID = "library-passages";

/** The open panel below the Library's toolbar; its toggle is the toolbar's. */
export function LibraryPassages({
  identity,
  namespaces = [],
  waiting = false,
  everywhere = false,
  unavailable = null,
  notSearched = 0,
  placeLabel,
  navEnabled,
  onHitsShown,
  onOpen,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  /** The other places searched beside `identity`'s, in a search everywhere. */
  namespaces?: string[];
  /** The places to search are still being read: a search waits for them. */
  waiting?: boolean;
  /** A search of every place the person reads, whatever is known of them yet. */
  everywhere?: boolean;
  /** Why the places to search are not known (the list's read failed): a search says it. */
  unavailable?: string | null;
  /** Places a search everywhere leaves out (refused, past the most one read takes). */
  notSearched?: number;
  /** A place's name, for the hits of a search of several: each says where it is. */
  placeLabel?: (namespace: string) => string;
  /** The arrows move through the hits (off while a dialog is open). */
  navEnabled: boolean;
  /** Told whether hits show, so the list below hands its row keys over to them. */
  onHitsShown: (shown: boolean) => void;
  /** Open the document a hit is from, in the place it is in. */
  onOpen: (sourceUri: string, namespace?: string) => void;
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
  // The places the hits on screen were searched in.
  const searchedFor = useRef("");
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

  // The search last asked for: run again on the new places when they change, or once they
  // are known, so the hits always answer the places named.
  const want = useRef<{ q: string; topK: number } | null>(null);
  // A search asked for while the places were still being read.
  const parked = useRef(false);
  const placesKey = [identity.org, identity.namespace, ...namespaces].join("\n");
  const search = async (q: string, topK: number) => {
    if (!q) return;
    want.current = { q, topK };
    searching.current?.abort();
    // Parked until the places are known; said, not spun, when they could not be read.
    parked.current = waiting || !!unavailable;
    setBusy(!unavailable);
    setError(unavailable ?? null);
    if (parked.current) return;
    searchedFor.current = placesKey;
    const ctrl = new AbortController();
    searching.current = ctrl;
    try {
      const found = await searchKnowledge(identity, q, topK, { signal: ctrl.signal, namespaces });
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

  // Runs when the places change or arrive (the search reads the latest render's).
  useEffect(() => {
    if (!want.current) return;
    if (unavailable) {
      if (parked.current) void search(want.current.q, TOP_K);
      return;
    }
    if (waiting) return;
    if (searchedFor.current === placesKey && !parked.current) return;
    void search(want.current.q, TOP_K);
  }, [placesKey, waiting, unavailable]);

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
                    onClick={() => onOpen(h.source_uri, h.namespace || undefined)}
                    className="list-row-open block w-full"
                  >
                    <span className="flex flex-wrap items-center gap-2 text-sm">
                      <span className="font-medium break-all">{name}</span>
                      {/* The whole address too, so two files of one name tell apart. */}
                      {name !== h.source_uri && (
                        <span className="min-w-0 truncate font-mono text-xs text-text-muted">{h.source_uri}</span>
                      )}
                      {h.section && <span className="text-text-muted">{h.section}</span>}
                      {placeLabel && h.namespace && (
                        <span className="text-text-muted">in {placeLabel(h.namespace)}</span>
                      )}
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
    [hits, busy, nav, onOpen, placeLabel],
  );

  return (
    <section id={PASSAGES_PANEL_ID} aria-label="Find a passage" className="mb-4 rounded-sm border border-border p-3">
      <div className="space-y-3">
        <p id={`${PASSAGES_PANEL_ID}-hint`} className="text-xs text-text-muted">
          {hint(everywhere, waiting || !!unavailable, namespaces.length + 1, notSearched)} Each passage is cited by the
          revision it was read from and its place.
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
