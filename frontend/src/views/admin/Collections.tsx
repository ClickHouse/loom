import { useDeferredValue, useMemo, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Table } from "@clickhouse/click-ui/Table";
import {
  deleteCollection,
  getNamespace,
  listCollections,
  listGrants,
  listKnowledge,
  type CollectionEntry,
  type GrantEntry,
  type KnowledgeEntry,
  type NamespaceInfo,
} from "../../api";
import { useAbortableEffect } from "../../lib/hooks";
import { useIdentity } from "../../lib/state";
import { useRowNav } from "../../lib/useRowNav";
import { humanizeError } from "../../lib/format";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast, toastError } from "../../components/Toast";
import { TableSkeleton } from "../../components/Skeleton";
import { BusyButton } from "../../components/ui/BusyButton";
import { ListState } from "../../components/ui/ListState";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { Modal, ModalFooter } from "../../components/ui/Modal";
import { RowMenu } from "../../components/ui/RowMenu";
import {
  Alert,
  ListTime,
  Loading,
  MetaList,
  MetaRow,
  PageHeader,
  SectionHeading,
  Unavailable,
} from "../../components/ui";
import { PageContainer } from "../../layout/PageContainer";
import { CreateCollectionDialog } from "../../flows/CreateCollectionDialog";
import { MembersDialog } from "../../flows/MembersDialog";

/**
 * Collections — the namespaces only grants open (docs/architecture/sharing.md), as many
 * of them as the caller's grants reach in the org. A row opens what the collection is,
 * who holds a grant on it and what it holds; its menu manages the members and deletes
 * it, which erases what it holds first (`collection.delete`).
 */
const HEADERS = [
  { label: "Name" },
  { label: "Inside" },
  { label: "Your role" },
  // The row menus' column: named for a screen reader, blank on screen.
  { label: <span className="sr-only">Actions</span>, width: "48px" },
];

/** How many of a collection's documents its details list. */
const DOCUMENTS_SHOWN = 25;

const ROLE_LABEL: Record<string, string> = { viewer: "Viewer", writer: "Writer", admin: "Admin" };

/** Why the caller cannot delete `c`, or null when they can. */
function deleteBlocked(c: CollectionEntry): string | null {
  if (c.personal) return "A personal collection is erased with its user";
  if (c.role !== "admin") return "Only an admin of this collection can delete it";
  return null;
}

/** Confirm, then delete `c` with what it holds. True once it is deleted. */
async function confirmDelete(org: string, c: CollectionEntry): Promise<boolean> {
  const ok = await confirmAction({
    title: `Delete ${c.namespace}?`,
    message:
      "Its documents, their original files and its memories are erased from disk, its grants stop applying, and the collections inside it become top-level. The name cannot be used again.",
    confirmLabel: "Delete collection",
    danger: true,
  });
  if (!ok) return false;
  try {
    const r = await deleteCollection(org, c.namespace);
    if (r.erasure_status && r.erasure_status !== "completed") {
      toast(`Deleted ${c.namespace}; erasing what it held is being retried`, "info", {
        detail: `erasure ${r.erasure_id}`,
      });
    } else {
      toast(`Deleted ${c.namespace}`, "ok");
    }
    return true;
  } catch (e) {
    toastError(`Couldn't delete ${c.namespace}`, e);
    return false;
  }
}

interface ListRead {
  org: string;
  rows: CollectionEntry[];
  next: string | null;
}

export function Collections() {
  const { org } = useIdentity();
  // The rows read, with the org they were read for: another org's are not shown.
  const [read, setRead] = useState<ListRead | null>(null);
  const current = read && read.org === org ? read : null;
  const rows = current?.rows ?? null;
  const [error, setError] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [open, setOpen] = useState<CollectionEntry | null>(null);
  const [members, setMembers] = useState<CollectionEntry | null>(null);

  // Refresh, Retry, a create and a delete read again through the effect, so a newer read
  // drops an older one.
  const [reads, setReads] = useState(0);
  const reload = () => setReads((n) => n + 1);
  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setRefreshing(true);
      listCollections(org, { signal })
        .then((r) => {
          if (live()) setRead({ org, rows: r.collections, next: r.next_cursor });
        })
        .catch((e) => {
          if (live()) setError(e);
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [org, reads],
  );

  const loadMore = () => {
    if (!current?.next) return;
    const from = current;
    setLoadingMore(true);
    listCollections(org, { cursor: from.next })
      .then((r) =>
        // Dropped when a newer read replaced the page it continues.
        setRead((now) => (now === from ? { org, rows: [...from.rows, ...r.collections], next: r.next_cursor } : now)),
      )
      .catch((e) => toastError("Couldn't load more collections", e))
      .finally(() => setLoadingMore(false));
  };

  const listQuery = useDeferredValue(query).trim().toLowerCase();
  const shown = useMemo(
    () => (rows ?? []).filter((c) => !listQuery || c.namespace.toLowerCase().includes(listQuery)),
    [rows, listQuery],
  );
  // A new collection may sit in one the caller writes; a personal one holds none.
  const parents = useMemo(() => (rows ?? []).filter((c) => !c.personal && c.role !== "viewer"), [rows]);
  const nav = useRowNav({ enabled: open === null && shown.length > 0 });

  const onDelete = async (c: CollectionEntry) => {
    if (await confirmDelete(org, c)) {
      setOpen(null);
      reload();
    }
  };

  const more = !!current?.next;
  const count =
    !error && rows !== null
      ? `${listQuery ? `${shown.length} of ` : ""}${rows.length}${more ? "+" : ""} collection${rows.length === 1 && !more ? "" : "s"}`
      : undefined;

  return (
    <PageContainer>
      <PageHeader
        title="Collections"
        actions={
          <>
            <BusyButton type="secondary" htmlType="button" iconLeft="refresh" onClick={reload} busy={refreshing}>
              Refresh
            </BusyButton>
            <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setCreating(true)}>
              New collection
            </Button>
          </>
        }
      />
      <p className="text-sm text-text-muted mb-4 max-w-[72ch]">
        Shared knowledge that only grants open. A collection inside another inherits its grants. These are the
        collections your grants reach in this org.
      </p>

      <ListToolbar label="Collections" count={count}>
        <div className="min-w-0 flex-1">
          <SearchField aria-label="Search collections" value={query} onChange={setQuery} placeholder="Search by name" />
        </div>
      </ListToolbar>

      {!!error && <ListState kind="error" things="collections" error={error} onRetry={reload} />}
      {!error && rows === null && (
        <Table
          aria-label="Collections"
          aria-busy
          size="sm"
          headers={HEADERS}
          rows={[]}
          noDataMessage={<TableSkeleton label="Loading collections" />}
        />
      )}
      {!error && rows !== null && rows.length === 0 && (
        <ListState
          kind="empty"
          icon="folder-closed"
          title="No collections yet"
          description="A collection holds documents that people, groups or agents read through grants. Create one, then add its members."
        />
      )}
      {!error && rows !== null && rows.length > 0 && shown.length === 0 && (
        <ListState kind="filtered" things="collections" filter={query.trim()} onClear={() => setQuery("")} />
      )}
      {!error && shown.length > 0 && (
        <div ref={nav} data-testid="collections-table" aria-busy={refreshing || undefined}>
          <Table
            aria-label="Collections"
            size="sm"
            headers={HEADERS}
            rows={shown.map((c) => {
              const blocked = deleteBlocked(c);
              return {
                id: c.namespace,
                items: [
                  {
                    label: (
                      <span className="inline-flex min-w-0 items-center gap-2">
                        <button
                          type="button"
                          data-row-open=""
                          onClick={() => setOpen(c)}
                          className="min-w-0 truncate rounded text-left font-mono text-xs"
                        >
                          {c.namespace}
                        </button>
                        {c.personal && <Badge text="Personal" state="neutral" size="sm" />}
                      </span>
                    ),
                  },
                  {
                    label: c.parent ? (
                      <span className="font-mono text-xs">{c.parent}</span>
                    ) : (
                      <span className="text-text-muted">Top level</span>
                    ),
                  },
                  { label: ROLE_LABEL[c.role] ?? c.role },
                  {
                    label: (
                      <RowMenu
                        name={c.namespace}
                        items={[
                          { label: "Open", icon: "eye", onSelect: () => setOpen(c) },
                          ...(c.personal
                            ? []
                            : [{ label: "Members", icon: "users" as const, onSelect: () => setMembers(c) }]),
                          ...(blocked
                            ? []
                            : [
                                {
                                  label: "Delete",
                                  icon: "trash" as const,
                                  danger: true,
                                  onSelect: () => void onDelete(c),
                                },
                              ]),
                        ]}
                      />
                    ),
                  },
                ],
              };
            })}
          />
        </div>
      )}
      {!error && more && (
        <div className="mt-3 flex justify-center">
          <BusyButton type="secondary" htmlType="button" onClick={loadMore} busy={loadingMore}>
            Load more
          </BusyButton>
        </div>
      )}

      <CreateCollectionDialog
        open={creating}
        onOpenChange={setCreating}
        org={org}
        parents={parents}
        onCreated={reload}
      />
      {open && (
        <CollectionDetails
          org={org}
          collection={open}
          onClose={() => setOpen(null)}
          onDelete={() => void onDelete(open)}
        />
      )}
      {members && (
        <MembersDialog
          open
          onOpenChange={(v) => {
            if (!v) setMembers(null);
          }}
          org={org}
          namespace={members.namespace}
        />
      )}
    </PageContainer>
  );
}

interface Details {
  key: string;
  info: NamespaceInfo | null;
  grants: GrantEntry[];
  documents: KnowledgeEntry[];
  moreDocuments: boolean;
}

/** One collection: what it is, who holds a grant on it, and the documents it holds. */
function CollectionDetails({
  org,
  collection,
  onClose,
  onDelete,
}: {
  org: string;
  collection: CollectionEntry;
  onClose: () => void;
  onDelete: () => void;
}) {
  const ns = collection.namespace;
  const key = `${org}/${ns}`;
  const [details, setDetails] = useState<Details | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reads, setReads] = useState(0);
  useAbortableEffect(
    (signal, live) => {
      setError(null);
      Promise.all([
        getNamespace({ org, namespace: ns }, { signal }),
        listGrants({ org, namespace: ns }, { signal }),
        listKnowledge(
          { org, namespace: ns },
          { limit: DOCUMENTS_SHOWN, fields: ["source_uri", "title", "ingested_at"], signal },
        ),
      ])
        .then(([info, grants, docs]) => {
          if (!live()) return;
          setDetails({
            key,
            info,
            grants: grants.rows ?? [],
            documents: docs.rows ?? [],
            moreDocuments: !!docs.next_cursor,
          });
        })
        .catch((e) => {
          if (live()) setError(humanizeError(e));
        });
    },
    [key, reads],
  );
  // Another collection's details are never shown under this one's title.
  const shown = details?.key === key ? details : null;
  const blocked = deleteBlocked(collection);

  return (
    <Modal
      open
      onOpenChange={(v) => {
        if (!v) onClose();
      }}
      title={ns}
      description={shown?.info?.description || undefined}
      className="w-full max-w-[640px]"
    >
      <MetaList className="mb-4">
        <MetaRow label="Inside" mono={!!collection.parent}>
          {collection.parent || "Top level"}
        </MetaRow>
        <MetaRow label="Your role">{ROLE_LABEL[collection.role] ?? collection.role}</MetaRow>
        {shown?.info?.created_at && (
          <MetaRow label="Created">
            <ListTime time={shown.info.created_at} />
          </MetaRow>
        )}
      </MetaList>

      {error && (
        <div className="space-y-2">
          <Alert role="alert" state="danger" text={`Couldn't load this collection: ${error}`} />
          <Button type="secondary" htmlType="button" onClick={() => setReads((n) => n + 1)}>
            Retry
          </Button>
        </div>
      )}
      {!error && !shown && <Loading label="Loading the collection" />}
      {!error && shown && (
        <div className="space-y-4">
          <section aria-labelledby="collection-members-heading">
            <SectionHeading
              id="collection-members-heading"
              label="Members"
              count={shown.grants.length}
              hint={
                collection.parent
                  ? `These are the grants on this collection. Everyone who holds a grant on ${collection.parent} reaches it too.`
                  : undefined
              }
            />
            {shown.grants.length === 0 ? (
              <p className="text-sm text-text-muted">
                {collection.personal ? "A personal collection admits its user alone." : "No grants on it."}
              </p>
            ) : (
              <ul className="space-y-1 text-sm">
                {shown.grants.map((g) => (
                  <li key={g.subject} className="flex min-w-0 items-center justify-between gap-3">
                    <span className="min-w-0 truncate font-mono text-xs">{g.subject}</span>
                    <span className="shrink-0 text-text-muted">
                      {ROLE_LABEL[g.role] ?? g.role}
                      {g.status === "pending" ? ", pending" : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section aria-labelledby="collection-documents-heading">
            <SectionHeading
              id="collection-documents-heading"
              label="Documents"
              caption={shown.moreDocuments ? `the newest ${DOCUMENTS_SHOWN}` : `${shown.documents.length} in all`}
            />
            {shown.documents.length === 0 ? (
              <p className="text-sm text-text-muted">It holds no documents.</p>
            ) : (
              <ul className="space-y-1 text-sm">
                {shown.documents.map((d) => (
                  <li key={d.source_uri} className="flex min-w-0 items-center justify-between gap-3">
                    <span className="min-w-0 truncate">{d.title || d.source_uri}</span>
                    <ListTime time={d.ingested_at} quiet className="shrink-0 text-text-muted" />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}

      <ModalFooter>
        <Unavailable reason={blocked}>
          <Button type="danger" htmlType="button" iconLeft="trash" onClick={onDelete}>
            Delete
          </Button>
        </Unavailable>
        <Button type="secondary" htmlType="button" onClick={onClose}>
          Close
        </Button>
      </ModalFooter>
    </Modal>
  );
}
