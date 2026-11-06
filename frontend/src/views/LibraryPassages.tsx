/**
 * Finding a passage inside the Library's documents (`knowledge.search`), each hit cited
 * as an agent would cite it: its document, the revision it was read from and where it
 * sits (lines, a key, pages or slides, a sheet's rows). A hit opens its document.
 */
import { useEffect, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Icon } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { type Identity, type KnowledgeHit, searchKnowledge } from "../api";
import { Alert } from "../components/ui/Alert";
import { BusyButton } from "../components/ui/BusyButton";
import { ListRow } from "../components/ui/ListRow";
import { humanizeError } from "../lib/format";
import { citation } from "../lib/locator";
import { useRowNav } from "../lib/useRowNav";

// The passages one search lists: the best this many.
const TOP_K = 10;

// What a hit shows of its passage; the document holds the rest.
const EXCERPT_CHARS = 280;

function excerpt(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > EXCERPT_CHARS ? `${flat.slice(0, EXCERPT_CHARS - 1)}…` : flat;
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
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const searching = useRef<AbortController | null>(null);
  const shown = open && !error && !!hits?.length;
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
      const found = await searchKnowledge(identity, q, { signal: ctrl.signal, topK: TOP_K });
      if (!ctrl.signal.aborted) setHits(found);
    } catch (e) {
      if (!ctrl.signal.aborted) setError(humanizeError(e));
    } finally {
      if (!ctrl.signal.aborted) setBusy(false);
    }
  };

  return (
    <div className="mb-4 rounded-sm border border-border">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm"
      >
        <Icon name={open ? "chevron-down" : "chevron-right"} size="sm" aria-hidden="true" />
        <span className="font-medium">Find a passage</span>
        <span className="truncate text-xs text-text-muted">
          inside this namespace’s documents, cited by revision and place
        </span>
      </button>
      {open && (
        <div className="space-y-3 px-3 pb-3">
          <form
            aria-label="Find a passage"
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
              />
            </div>
            <BusyButton type="secondary" htmlType="submit" disabled={!query.trim()} busy={busy}>
              Find
            </BusyButton>
          </form>
          {error && <Alert role="alert" state="danger" text={error} />}
          {hits && !error && (
            <div aria-live="polite">
              {hits.length === 0 ? (
                <p className="text-sm text-text-muted">No passage answers that.</p>
              ) : (
                <>
                  <p className="mb-2 text-xs text-text-muted">
                    {hits.length >= TOP_K
                      ? `The best ${TOP_K} passages`
                      : `${hits.length} passage${hits.length === 1 ? "" : "s"}`}
                  </p>
                  <ol ref={nav} className="space-y-2" aria-label="Passages found">
                    {hits.map((h) => (
                      <li key={`${h.source_uri}#${h.chunk_idx}`}>
                        <ListRow>
                          <button
                            type="button"
                            data-row-open=""
                            onClick={() => onOpen(h.source_uri)}
                            className="list-row-open block w-full"
                          >
                            <span className="flex flex-wrap items-center gap-2 text-sm">
                              <span className="font-medium break-all">{shortName(h.source_uri)}</span>
                              {h.section && <span className="text-text-muted">{h.section}</span>}
                              {citation(h) && <Badge size="sm" state="neutral" text={citation(h)} />}
                              {(h.keys_matched ?? []).map((k) => (
                                <Badge key={k} size="sm" state="info" text={k} />
                              ))}
                            </span>
                            <span className="mt-1 block text-sm text-text-muted">{excerpt(h.content)}</span>
                          </button>
                        </ListRow>
                      </li>
                    ))}
                  </ol>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
