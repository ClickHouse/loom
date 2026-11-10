/**
 * A repository's documents on its card: the design notes, runbooks and specs its code
 * depends on. They are knowledge documents in the repository's docs namespace (its trunk
 * branch's), so they carry the repository's access: a reader reads them, a writer there
 * adds and removes them. Agents find them where they work — `loom_search` in the
 * repository's checkout, and the orientation context pack.
 *
 * Each title opens the document; each row says how often agents opened it
 * this week (`knowledge.reads`: agent `knowledge.get` calls, the dashboard's
 * own previews left out) — "opened", because what a search returned is not
 * recorded. Read once when the card is opened (a folded card asks nothing);
 * the dialogs load on first use.
 */
import { lazy, Suspense, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { deleteKnowledge, knowledgeReads, listKnowledge, type KnowledgeEntry } from "../api";
import { ApiError } from "../lib/errors";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import { confirmAction } from "../components/ConfirmDialog";
import { Skeleton } from "../components/Skeleton";
import { toast, toastError } from "../components/Toast";
import { ListTime, LoadingSkeleton } from "../components/ui";

const RepoDocAddDialog = lazy(() => import("../flows/RepoDocDialogs").then((m) => ({ default: m.RepoDocAddDialog })));
const RepoDocViewer = lazy(() => import("../flows/RepoDocDialogs").then((m) => ({ default: m.RepoDocViewer })));

export const REPO_DOCS_HINT =
  "Agents working in this repository's checkout find these through loom_search, and its orientation context pack lists them.";
const FIELDS = ["source_uri", "title", "description", "ingested_at", "has_blob", "original_filename"];
const LIMIT = 20;
const READS_DAYS = 7;

type State =
  | { kind: "loading" }
  | { kind: "ready"; rows: KnowledgeEntry[]; more: boolean }
  | { kind: "refused" }
  | { kind: "error"; message: string };

/** "opened by agents 5× this week" / "not opened by agents this week". */
export function readsPhrase(n: number | undefined): string {
  if (n === undefined) return "";
  return n > 0 ? `opened by agents ${n}× this week` : "not opened by agents this week";
}

export function RepoDocsList({
  org,
  docsNamespace,
  repo,
  writable,
  busy,
}: {
  org: string;
  docsNamespace: string;
  repo: string;
  /** The caller may write the docs namespace: add and remove. */
  writable: boolean;
  busy: boolean;
}) {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [reads, setReads] = useState<Record<string, number> | null>(null);
  const [nonce, setNonce] = useState(0);
  const [adding, setAdding] = useState(false);
  const [viewing, setViewing] = useState<KnowledgeEntry | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const id = { org, namespace: docsNamespace };

  useAbortableEffect(
    async (signal, live) => {
      let rows: KnowledgeEntry[];
      try {
        const r = await listKnowledge(id, { limit: LIMIT + 1, fields: FIELDS, signal });
        if (!live()) return;
        rows = r.rows.slice(0, LIMIT);
        setState({ kind: "ready", rows, more: r.rows.length > LIMIT || !!r.next_cursor });
      } catch (e) {
        if (!live()) return;
        if (e instanceof ApiError && (e.status === 403 || e.status === 401)) setState({ kind: "refused" });
        else setState({ kind: "error", message: humanizeError(e, { read: true }) });
        return;
      }
      if (rows.length === 0) {
        setReads({});
        return;
      }
      try {
        const r = await knowledgeReads(id, { days: READS_DAYS, source_uris: rows.map((d) => d.source_uri), signal });
        if (live()) setReads(Object.fromEntries(r.reads.map((x) => [x.source_uri, x.reads])));
      } catch {
        if (live()) setReads(null); /* no read counts: nothing else changes */
      }
    },
    [org, docsNamespace, nonce],
  );

  const remove = async (doc: KnowledgeEntry) => {
    if (
      !(await confirmAction({
        title: `Remove ${doc.title || doc.source_uri}?`,
        message: "Agents stop finding it at once. You can add it again.",
        confirmLabel: "Remove",
        danger: true,
      }))
    )
      return;
    setRemoving(doc.source_uri);
    try {
      await deleteKnowledge(id, doc.source_uri);
      toast(`Removed ${doc.title || doc.source_uri}`, "ok");
      setNonce((n) => n + 1);
    } catch (e) {
      toastError("Couldn't remove it", e);
    } finally {
      setRemoving(null);
    }
  };

  const countOf = (uri: string) => (reads ? (reads[uri] ?? 0) : undefined);
  return (
    <div className="space-y-1.5">
      {state.kind === "loading" && (
        <LoadingSkeleton label="Loading the repository's documents">
          <div className="space-y-2">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        </LoadingSkeleton>
      )}
      {state.kind === "refused" && (
        <p className="text-xs text-text-muted">Join the repository to read its documents.</p>
      )}
      {state.kind === "error" && (
        <p className="text-xs text-text-muted" role="status">
          Couldn't list the documents — {state.message}
        </p>
      )}
      {state.kind === "ready" && state.rows.length === 0 && (
        <p className="text-xs text-text-muted">
          No documents yet.{" "}
          {writable
            ? "Add the design notes, runbooks or specs agents should read before changing this code."
            : "Anyone who can write to the repository adds them."}
        </p>
      )}
      {state.kind === "ready" && state.rows.length > 0 && (
        <ul className="space-y-1" aria-label="Repository documents">
          {state.rows.map((d) => {
            const phrase = readsPhrase(countOf(d.source_uri));
            return (
              <li key={d.source_uri} className="flex items-start gap-2 text-sm min-w-0">
                <Icon name="document" size="xs" aria-hidden="true" className="shrink-0 mt-1" />
                <div className="min-w-0 flex-1">
                  <Link
                    component="button"
                    type="button"
                    size="md"
                    onClick={() => setViewing(d)}
                    className="text-left min-h-6 max-w-full truncate block"
                  >
                    {d.title || d.source_uri}
                  </Link>
                  <div className="text-xs text-text-muted truncate">
                    {d.description ? <>{d.description} · </> : null}
                    {d.ingested_at ? (
                      <>
                        added <ListTime time={d.ingested_at} quiet />
                      </>
                    ) : null}
                    {phrase ? (
                      <>
                        {" "}
                        · <span className={countOf(d.source_uri) ? "text-text" : ""}>{phrase}</span>
                      </>
                    ) : null}
                  </div>
                </div>
                {writable && (
                  <Button
                    type="empty"
                    htmlType="button"
                    className="shrink-0"
                    loading={removing === d.source_uri}
                    disabled={busy || removing !== null}
                    onClick={() => void remove(d)}
                    aria-label={`Remove ${d.title || d.source_uri}`}
                  >
                    Remove
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {state.kind === "ready" && state.more && <p className="text-xs text-text-muted">Showing the first {LIMIT}.</p>}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {writable && state.kind !== "refused" && (
          <Button type="empty" htmlType="button" iconLeft="plus" disabled={busy} onClick={() => setAdding(true)}>
            Add a document
          </Button>
        )}
        {state.kind === "ready" && (state.rows.length > 0 || writable) && (
          <span className="text-xs text-text-muted">{REPO_DOCS_HINT}</span>
        )}
      </div>
      {adding && (
        <Suspense fallback={null}>
          <RepoDocAddDialog
            open
            org={org}
            namespace={docsNamespace}
            repo={repo}
            takenUris={state.kind === "ready" ? state.rows.map((r) => r.source_uri) : []}
            onOpenChange={(v) => {
              if (!v) setAdding(false);
            }}
            onAdded={(title, lost) => {
              if (lost === null) toast(`Added ${title}`, "ok");
              else
                toast(
                  `Added ${title}, partial: part of the file could not be read${lost.length ? ` (lost: ${lost.join(", ")})` : ""}`,
                  "info",
                );
              setNonce((n) => n + 1);
            }}
          />
        </Suspense>
      )}
      {viewing && (
        <Suspense fallback={null}>
          <RepoDocViewer
            open
            org={org}
            namespace={docsNamespace}
            doc={viewing}
            reads={reads ? { reads: countOf(viewing.source_uri) ?? 0, days: READS_DAYS } : null}
            onOpenChange={(v) => {
              if (!v) setViewing(null);
            }}
          />
        </Suspense>
      )}
    </div>
  );
}
