/**
 * A namespace's document sources: the shared folders, project document stores, page
 * stores' pages and meeting transcripts its documents are pulled from, each with its
 * last sync. Shown on a repository's card (for its docs namespace) and on the Library
 * (for the namespace in view): a reader sees them and an admin manages them: Add a
 * source, Check, Sync now, Rotate credential, Retire.
 * Read once when the card is opened (a folded card asks nothing) or the Library's place
 * view opens, the read deciding whether its button shows; the dialogs load on first
 * use. The counts are information only: nothing waits for a person to confirm a row.
 */
import { lazy, Suspense, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Link } from "@clickhouse/click-ui/Link";
import { listKnowledgeSources } from "../api/knowledgeSources";
import { ApiError } from "../lib/errors";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import {
  kindLabel,
  SYNC_OFF,
  syncBadge,
  syncCounts,
  unconvertedDetail,
  type KnowledgeSource,
  type SyncTone,
} from "../lib/knowledgeSources";
import { isOpAbsent } from "../lib/trackerSources";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";
import { Skeleton } from "../components/Skeleton";
import { IconButton } from "../components/IconButton";
import { toast } from "../components/Toast";
import { ListTime, LoadingSkeleton, TipTarget } from "../components/ui";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";

const AddSourceDialog = lazy(() =>
  import("../flows/KnowledgeSourceDialog").then((m) => ({ default: m.AddSourceDialog })),
);
const CheckSourceDialog = lazy(() =>
  import("../flows/KnowledgeSourceDialog").then((m) => ({ default: m.CheckSourceDialog })),
);
const SyncNowDialog = lazy(() => import("../flows/KnowledgeSourceDialog").then((m) => ({ default: m.SyncNowDialog })));
const RotateSourceDialog = lazy(() =>
  import("../flows/KnowledgeSourceDialog").then((m) => ({ default: m.RotateSourceDialog })),
);
const SourceDetailsDialog = lazy(() =>
  import("../flows/SourceDetailsDialog").then((m) => ({ default: m.SourceDetailsDialog })),
);
const RetireSourceDialog = lazy(() =>
  import("../flows/KnowledgeSourceDialog").then((m) => ({ default: m.RetireSourceDialog })),
);

const TONE_STATE: Record<SyncTone, BadgeState> = { ok: "success", warn: "warning", danger: "danger", muted: "neutral" };

type State =
  | { kind: "loading" }
  | { kind: "ready"; sources: KnowledgeSource[]; enabled: boolean; accepts: boolean }
  | { kind: "absent" }
  | { kind: "refused" }
  | { kind: "error"; message: string };

type Verb = "check" | "sync" | "rotate" | "retire";

function SourceRow({ s, items, onOpen }: { s: KnowledgeSource; items: RowMenuItem[]; onOpen: () => void }) {
  const badge = syncBadge(s.sync);
  const counts = syncCounts(s.sync);
  const reasons = unconvertedDetail(s.sync.unconverted_reasons);
  return (
    <li className="flex items-start gap-2 text-sm min-w-0">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          {/* The name opens the source's details: its settings and the documents it wrote. */}
          <Link component="button" size="sm" className="font-mono" onClick={onOpen}>
            {s.name}
          </Link>
          <span className="text-xs text-text-muted">{kindLabel(s.kind)}</span>
          <TipTarget tip={badge.tip}>
            <Badge text={badge.text} state={TONE_STATE[badge.tone]} size="sm" />
          </TipTarget>
          {s.sync.last_synced_at && (
            <span className="text-xs text-text-muted">
              synced <ListTime time={s.sync.last_synced_at} quiet />
            </span>
          )}
          {s.sync.requested_at && (
            <span className="text-xs text-text-muted">
              sync requested <ListTime time={s.sync.requested_at} quiet />
            </span>
          )}
        </div>
        {counts && (
          <div className="text-xs text-text-muted tabular-nums">
            {reasons ? (
              <TipTarget tip={`Not converted: ${reasons}`} underline>
                {counts}
              </TipTarget>
            ) : (
              counts
            )}
          </div>
        )}
        {s.sync.error && s.sync.status !== "ok" && (
          <div className="text-xs text-text-muted break-words">{s.sync.error}</div>
        )}
      </div>
      {items.length > 0 && <RowMenu name={s.name} items={items} />}
    </li>
  );
}

/** The place a list belongs to: what the add dialog's title names, and what the copy calls it. */
export type SourcesPlace = { label: string; noun: "repository" | "namespace" | "collection" };

export function SourcesList({
  org,
  namespace,
  place,
  manageable,
  busy = false,
  section,
  panel,
}: {
  org: string;
  /** The namespace the sources live in. */
  namespace: string;
  place: SourcesPlace;
  /** The caller may call the source ops on the namespace: every verb. */
  manageable: boolean;
  /** Another change on the page is running: Add waits for it. */
  busy?: boolean;
  /** Render as the Library's own section under this heading: a button named for it, in
   *  the header's actions, that opens the section in `panel`, so the closed list takes no
   *  room above the rows. It shows only once the list has answered, and to a caller who
   *  cannot add, only while it lists a source. */
  section?: string;
  /** Where the open section goes (below the page header). */
  panel?: HTMLElement | null;
}) {
  const [state, setState] = useState<State>({ kind: "loading" });
  const headingId = useId();
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const phone = useMedia(PHONE_QUERY);
  const heading = useRef<HTMLHeadingElement>(null);
  // The panel sits below the header, past the page's other controls: opening it moves
  // the focus to its heading, so the list is next in the tab order.
  useEffect(() => {
    if (open) heading.current?.focus();
  }, [open]);
  const [nonce, setNonce] = useState(0);
  const [adding, setAdding] = useState(false);
  const [acting, setActing] = useState<{ verb: Verb; source: KnowledgeSource } | null>(null);
  // Details is a read: any reader may open it.
  const [details, setDetails] = useState<string | null>(null);
  const id = { org, namespace };
  const reload = () => setNonce((n) => n + 1);

  useAbortableEffect(
    async (signal, live) => {
      try {
        const r = await listKnowledgeSources(id, { signal });
        if (!live()) return;
        setState({ kind: "ready", sources: r.sources, enabled: r.enabled, accepts: r.accepts_sources !== false });
      } catch (e) {
        if (!live()) return;
        if (isOpAbsent(e)) setState({ kind: "absent" });
        else if (e instanceof ApiError && (e.status === 403 || e.status === 401)) setState({ kind: "refused" });
        else setState({ kind: "error", message: humanizeError(e, { read: true }) });
      }
    },
    [org, namespace, nonce],
  );

  const menu = (s: KnowledgeSource): RowMenuItem[] => {
    if (!manageable) return [];
    const act = (verb: Verb) => () => setActing({ verb, source: s });
    const check: RowMenuItem = { label: "Check", icon: "check", onSelect: act("check") };
    const sync: RowMenuItem = { label: "Sync now", icon: "refresh", onSelect: act("sync") };
    const rotate: RowMenuItem = { label: "Rotate credential", icon: "key", onSelect: act("rotate") };
    const retire: RowMenuItem = { label: "Retire", icon: "trash", danger: true, onSelect: act("retire") };
    // A place that takes no new source refuses a sync and a rotation: Check and Retire remain.
    const closed = state.kind === "ready" && !state.accepts;
    const items = closed ? [check, retire] : [check, sync, rotate, retire];
    return items.map((item) => ({ ...item, disabled: busy }));
  };
  const close = (v: boolean) => {
    if (!v) setActing(null);
  };

  // The Library's section hides while its list loads, on an empty place that takes
  // no source or that this caller cannot add to, and from a non-admin when the list failed.
  const empty = state.kind === "ready" && state.sources.length === 0;
  const closedAndEmpty = empty && !state.accepts;
  const nothingForReader = empty && !manageable;
  const loading = state.kind === "loading";
  const failedForReader = state.kind !== "ready" && !loading && !manageable;
  if (section && (closedAndEmpty || nothingForReader || loading || failedForReader)) return null;
  const list = (
    <div className="space-y-1.5">
      {state.kind === "loading" && (
        <LoadingSkeleton label={`Loading the ${place.noun}'s document sources`}>
          <Skeleton className="h-4 w-1/2" />
        </LoadingSkeleton>
      )}
      {state.kind === "absent" && (
        <p className="text-xs text-text-muted">Document sources are not available on this deployment yet.</p>
      )}
      {state.kind === "refused" && (
        <p className="text-xs text-text-muted">Join the {place.noun} to see where its documents come from.</p>
      )}
      {state.kind === "error" && (
        <p className="text-xs text-text-muted" role="status">
          Couldn't list the document sources — {state.message}
        </p>
      )}
      {state.kind === "ready" && !state.enabled && <p className="text-xs text-text-muted">{SYNC_OFF}</p>}
      {state.kind === "ready" && !state.accepts && (
        <p className="text-xs text-text-muted">
          This {place.noun} takes no new document sources
          {manageable && state.sources.length > 0 ? "; retire these when they are no longer needed." : "."}
        </p>
      )}
      {state.kind === "ready" && state.accepts && state.sources.length === 0 && (
        <p className="text-xs text-text-muted">
          No document sources.{" "}
          {manageable
            ? `Add a source and Loom keeps the ${place.noun}'s documents in step with it.`
            : `Anyone with admin on the ${place.noun} adds them.`}
        </p>
      )}
      {state.kind === "ready" && state.sources.length > 0 && (
        <ul className="space-y-1.5" aria-label="Document sources">
          {state.sources.map((s) => (
            <SourceRow key={s.name} s={s} items={menu(s)} onOpen={() => setDetails(s.name)} />
          ))}
        </ul>
      )}
      {manageable && state.kind === "ready" && state.accepts && (
        <Button type="empty" htmlType="button" iconLeft="plus" disabled={busy} onClick={() => setAdding(true)}>
          Add a source
        </Button>
      )}
      {details && (
        <Suspense fallback={null}>
          <SourceDetailsDialog org={org} namespace={namespace} name={details} onClose={() => setDetails(null)} />
        </Suspense>
      )}
      {adding && (
        <Suspense fallback={null}>
          <AddSourceDialog
            open
            org={org}
            namespace={namespace}
            label={place.label}
            onOpenChange={(v) => {
              if (!v) setAdding(false);
            }}
            onAdded={(name) => {
              toast(`Added ${name}`, "ok");
              reload();
            }}
          />
        </Suspense>
      )}
      {acting && (
        <Suspense fallback={null}>
          {acting.verb === "check" && (
            <CheckSourceDialog open org={org} namespace={namespace} name={acting.source.name} onOpenChange={close} />
          )}
          {acting.verb === "sync" && (
            <SyncNowDialog
              open
              org={org}
              namespace={namespace}
              name={acting.source.name}
              onOpenChange={close}
              onRequested={reload}
            />
          )}
          {acting.verb === "rotate" && (
            <RotateSourceDialog
              open
              org={org}
              namespace={namespace}
              name={acting.source.name}
              kind={acting.source.kind}
              onOpenChange={close}
              onRotated={() => {
                toast(`Replaced the credential of ${acting.source.name}`, "ok");
                reload();
              }}
            />
          )}
          {acting.verb === "retire" && (
            <RetireSourceDialog
              open
              org={org}
              namespace={namespace}
              name={acting.source.name}
              enabled={state.kind === "ready" && state.enabled}
              onOpenChange={close}
              onRetired={reload}
            />
          )}
        </Suspense>
      )}
    </div>
  );
  if (!section) return list;
  const count = state.kind === "ready" ? state.sources.length : 0;
  const name = count > 0 ? `${section} (${count})` : section;
  const toggle = { "aria-expanded": open, "aria-controls": panelId, onClick: () => setOpen((v) => !v) };
  return (
    <>
      {/* A disclosure toggle; its icon alone, with a tooltip, on a phone, where the header
          has no room for another label. */}
      {phone ? (
        <IconButton icon="connect" label={name} {...toggle} />
      ) : (
        <Button type="empty" htmlType="button" iconRight={open ? "chevron-up" : "chevron-down"} {...toggle}>
          {name}
        </Button>
      )}
      {open &&
        panel &&
        createPortal(
          <section id={panelId} aria-labelledby={headingId} className="mb-6">
            <h2 id={headingId} ref={heading} tabIndex={-1} className="mb-2 text-sm font-semibold text-text">
              {section}
            </h2>
            {list}
          </section>,
          panel,
        )}
    </>
  );
}
