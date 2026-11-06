/**
 * Finding a passage inside the Library's documents (`knowledge.search`), each hit cited
 * as an agent would cite it: its document, the revision it was read from and where it
 * sits (lines, a key, pages or slides, a sheet's rows). A hit opens its document.
 */
import { useEffect, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { type Identity, type KnowledgeHit, searchKnowledge } from "../api";
import { Alert } from "../components/ui/Alert";
import { BusyButton } from "../components/ui/BusyButton";
import { ListRow } from "../components/ui/ListRow";
import { humanizeError, truncate } from "../lib/format";
import { citation } from "../lib/locator";
import { useRowNav } from "../lib/useRowNav";

// The passages one search lists: the best this many.
const TOP_K = 10;

// What a hit shows of its passage; the document holds the rest.
const EXCERPT_CHARS = 280;

function excerpt(text: string): string {
  return truncate(text.replace(/\s+/g, " ").trim(), EXCERPT_CHARS);
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const searching = useRef<AbortController | null>(null);
  // A search still running when the panel goes (another place) is dropped.
  useEffect(() => () => searching.current?.abort(), []);
  const shown = !error && !!hits?.length;
  const nav = useRowNav({ enabled: navEnabled && shown });
  useEffect(() => {
    onHitsShown(shown);
    return () => onHitsShown(false);
  }, [shown, onHitsShown]);

  const search = async () => {
    const q = query.trim();
    if (!q) return;
    searching.current?.abort();
    const ctrl = new AbortController();
    searching.current = ctrl;
    setBusy(true);
    setError(null);
    try {
      const found = await searchKnowledge(identity, q, TOP_K, { signal: ctrl.signal });
      if (!ctrl.signal.aborted) setHits(found);
    } catch (e) {
      if (!ctrl.signal.aborted) setError(humanizeError(e));
    } finally {
      if (!ctrl.signal.aborted) setBusy(false);
    }
  };

  const count =
    hits === null || error
      ? ""
      : hits.length === 0
        ? "No passage answers that."
        : hits.length >= TOP_K
          ? `The best ${TOP_K} passages`
          : `${hits.length} passage${hits.length === 1 ? "" : "s"}`;

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
            void search();
          }}
        >
          <div className="min-w-0 flex-1">
            <SearchField
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
        {shown && hits && (
          <ol ref={nav} className="space-y-2" aria-label="Passages found" aria-busy={busy || undefined}>
            {hits.map((h, i) => {
              const cited = citation(h);
              const name = shortName(h.source_uri);
              const excerptId = `${PASSAGES_PANEL_ID}-excerpt-${i}`;
              return (
                <li key={`${h.source_uri}#${h.chunk_idx}`}>
                  <ListRow>
                    <button
                      type="button"
                      data-row-open=""
                      // Named by its document and citation; the excerpt is its description.
                      aria-label={`Open ${h.source_uri}${cited ? `, ${cited}` : ""}`}
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
                        {(h.keys_matched ?? []).map((k) => (
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
        )}
      </div>
    </section>
  );
}
