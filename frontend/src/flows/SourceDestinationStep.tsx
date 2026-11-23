import { Button } from "@clickhouse/click-ui/Button";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { useEffect, useId, useRef, useState } from "react";
import { type CollectionEntry, listCollections } from "../api";
import { Alert } from "../components/ui/Alert";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { ListState } from "../components/ui/ListState";
import { Loading } from "../components/ui/Loading";
import { SectionHeading } from "../components/ui/SectionHeading";
import { onCollectionsChanged } from "../lib/collectionPlaces";
import { canHold, COLLECTIONS_PAGE, COLLECTIONS_PAGES, collectionOrder, collectionTitle, titleKey } from "../lib/collections";
import { useAbortableEffect } from "../lib/hooks";
import { isCollectionName, isPersonalCollection } from "../lib/namespace";
import { CreateCollectionDialog } from "./CreateCollectionDialog";

/** Supplied by the setup flow so this isolated step introduces no translation catalog. */
export interface SourceDestinationCopy {
  title: string;
  description: string;
  searchLabel: string;
  searchHint: string;
  pageScope: string;
  destinationsLabel: string;
  loading: string;
  loadError: string;
  things: string;
  empty: string;
  previous: string;
  next: string;
  refresh: string;
  create: string;
  createHint: string;
  topLevelHint: string;
  selectionHint: string;
  pagingError: string;
  pagingLimit: string;
  inheritanceHint: (parent: string) => string;
  created: (namespace: string) => string;
  selected: (namespace: string) => string;
  page: (number: number) => string;
}

export type SourceDestinationInvalidation = "context_changed" | "collections_changed" | "selection_changed" | "refresh";

export interface SourceDestinationProps {
  org: string;
  /** Authenticated caller/auth revision, not an email or a source account address. */
  callerKey: string;
  /** Parent changes this when connection, selected content or mode changes. */
  contextKey: string;
  /** Parent changes this for grant/parent changes not covered by collection events. */
  collectionVersion?: string | number;
  value: CollectionEntry | null;
  onChange: (destination: CollectionEntry | null) => void;
  /** Clears the parent's effective-access preview; this step never grants authority. */
  onInvalidated: (reason: SourceDestinationInvalidation) => void;
  /** Exact confirmed identity, including when the next catalog read fails. Not auto-selected. */
  onCollectionCreated: (created: { org: string; callerKey: string; namespace: string }) => void;
  copy: SourceDestinationCopy;
  disabled?: boolean;
}

function writableOrdinary(c: CollectionEntry): boolean {
  return (
    canHold(c) &&
    (c.role === "writer" || c.role === "admin") &&
    isCollectionName(c.namespace) &&
    !isPersonalCollection(c.namespace)
  );
}

/** Destination choice only. Collection grants do not delegate the source connection. */
export function SourceDestinationStep(props: SourceDestinationProps) {
  const scope = JSON.stringify([props.org, props.callerKey, props.contextKey, props.collectionVersion ?? 0]);
  const previous = useRef(scope);
  const callbacks = useRef(props);
  callbacks.current = props;
  const changed = previous.current !== scope;
  useEffect(() => {
    const wasChanged = previous.current !== scope;
    previous.current = scope;
    callbacks.current.onInvalidated(wasChanged ? "context_changed" : "refresh");
    if (wasChanged) callbacks.current.onChange(null);
  }, [scope]);
  return <DestinationChoices key={scope} {...props} value={changed ? null : props.value} />;
}

function DestinationChoices(props: SourceDestinationProps) {
  const { org, callerKey, copy, disabled = false } = props;
  const titleId = useId();
  const hintId = useId();
  const searchId = useId();
  const [query, setQuery] = useState("");
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState(0);
  const [revision, setRevision] = useState(0);
  const [answer, setAnswer] = useState<{ rows: CollectionEntry[]; next: string | null } | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [createdNamespace, setCreatedNamespace] = useState<string | null>(null);
  const current = useRef(props);
  current.current = props;
  const createVersion = useRef(0);
  const cursor = cursors[page];

  const reload = () => {
    current.current.onInvalidated("refresh");
    if (current.current.value) current.current.onChange(null);
    setBusy(true);
    setCursors([null]);
    setPage(0);
    setRevision((r) => r + 1);
  };
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useEffect(() => {
    const unsubscribe = onCollectionsChanged((made) => {
      if (!made || made.org === org) reloadRef.current();
    });
    const focus = () => reloadRef.current();
    window.addEventListener("focus", focus);
    return () => {
      unsubscribe();
      window.removeEventListener("focus", focus);
    };
  }, [org]);

  useAbortableEffect(
    async (signal, live) => {
      if (signal.aborted) return;
      setBusy(true);
      setFailed(false);
      try {
        const result = await listCollections(org, { cursor, limit: COLLECTIONS_PAGE, signal });
        if (!live()) return;
        // Repeated rows from replicas are displayed once, using the last metadata read.
        const rows = [...new Map(result.collections.map((c) => [c.namespace, c])).values()];
        setAnswer({ rows, next: result.next_cursor });
        setBusy(false);
      } catch {
        if (live()) {
          setFailed(true);
          setBusy(false);
        }
      }
    },
    [org, cursor, revision],
  );

  useEffect(() => {
    const selected = props.value;
    if (!selected) return;
    const found = answer?.rows.find((c) => c.namespace === selected.namespace);
    if (
      !writableOrdinary(selected) ||
      (found && (!writableOrdinary(found) || found.parent !== selected.parent || found.role !== selected.role))
    ) {
      current.current.onInvalidated("collections_changed");
      current.current.onChange(null);
    }
  }, [answer, props.value]);

  const lastPage = useRef(page);
  useEffect(() => {
    if (answer && !busy && lastPage.current !== page) {
      document.getElementById(titleId)?.focus();
      lastPage.current = page;
    }
  }, [answer, busy, page, titleId]);

  const rows = (answer?.rows ?? []).filter(writableOrdinary).sort(collectionOrder);
  const text = titleKey(query.trim());
  const visible = rows.filter((c) => titleKey(`${collectionTitle(c)} ${c.namespace}`).includes(text));
  const repeatedCursor = !!answer?.next && cursors.slice(0, page + 1).includes(answer.next);
  const pageLimit = !!answer?.next && page + 1 >= COLLECTIONS_PAGES;
  const choose = (namespace: string) => {
    const row = visible.find((c) => c.namespace === namespace);
    if (!row || disabled || busy) return;
    props.onInvalidated("selection_changed");
    props.onChange(row);
  };
  const goTo = (index: number) => {
    setAnswer(null);
    setBusy(true);
    setFailed(false);
    setPage(index);
  };

  return (
    <section aria-labelledby={titleId} className="min-w-0 space-y-4 break-words">
      <SectionHeading id={titleId} label={copy.title} focusable />
      <p className="text-sm text-text-muted">{copy.description}</p>
      <SearchField
        id={searchId}
        label={copy.searchLabel}
        value={query}
        onChange={setQuery}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.preventDefault();
        }}
        disabled={disabled}
        aria-describedby={hintId}
      />
      <p id={hintId} className="text-xs text-text-muted">
        {copy.searchHint}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button type="secondary" htmlType="button" disabled={disabled || busy} onClick={() => setCreateOpen(true)}>
          {copy.create}
        </Button>
        <Button type="secondary" htmlType="button" disabled={disabled} onClick={reload}>
          {copy.refresh}
        </Button>
      </div>
      <p className="text-xs text-text-muted">{copy.createHint}</p>
      {createdNamespace && <Alert role="status" state="info" text={copy.created(createdNamespace)} />}
      {props.value && <p>{copy.selected(props.value.namespace)}</p>}
      {failed ? (
        <ListState kind="error" things={copy.things} error={copy.loadError} onRetry={reload} />
      ) : !answer ? (
        <Loading label={copy.loading} />
      ) : (
        <div aria-busy={busy}>
          {visible.length ? (
            <ChoiceGroup
              label={copy.destinationsLabel}
              value={props.value?.namespace ?? ""}
              onChange={choose}
              disabled={disabled || busy}
              options={visible.map((c) => ({
                value: c.namespace,
                label: `${collectionTitle(c)} (${c.namespace})`,
                hint: c.parent ? copy.inheritanceHint(c.parent) : copy.topLevelHint,
              }))}
            />
          ) : text ? (
            <ListState
              kind="filtered"
              things={copy.things}
              filter={query}
              among={copy.pageScope}
              onClear={() => {
                setQuery("");
                document.getElementById(searchId)?.focus();
              }}
            />
          ) : (
            <ListState kind="empty" icon="info-in-circle" title={copy.empty} />
          )}
          {repeatedCursor && <Alert role="alert" state="danger" text={copy.pagingError} />}
          {pageLimit && !repeatedCursor && <Alert role="status" state="info" text={copy.pagingLimit} />}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="secondary" htmlType="button" disabled={disabled || page === 0} onClick={() => goTo(page - 1)}>
          {copy.previous}
        </Button>
        <span role="status">{copy.page(page + 1)}</span>
        <Button
          type="secondary"
          htmlType="button"
          disabled={disabled || busy || !answer?.next || repeatedCursor || pageLimit}
          onClick={() => {
            if (busy || !answer?.next || repeatedCursor || pageLimit) return;
            setCursors((before) => [...before.slice(0, page + 1), answer.next]);
            goTo(page + 1);
          }}
        >
          {copy.next}
        </Button>
      </div>
      <p className="text-sm text-text-muted">{copy.selectionHint}</p>
      {createOpen && !disabled && (
        <CreateCollectionDialog
          open
          onOpenChange={setCreateOpen}
          org={org}
          parents={rows}
          claimCreateVersion={() => {
            props.onInvalidated("collections_changed");
            return ++createVersion.current;
          }}
          onCreated={(namespace, version) => {
            if (version !== createVersion.current) return;
            setCreatedNamespace(namespace);
            props.onCollectionCreated({ org, callerKey, namespace });
          }}
        />
      )}
    </section>
  );
}
