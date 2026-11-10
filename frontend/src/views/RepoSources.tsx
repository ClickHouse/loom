/**
 * A repository's document sources on its card: the shared folders, project document
 * stores, page stores' pages and meeting transcripts its documents are pulled from,
 * each with its last sync. They live on the repository's trunk code namespace, the
 * docs namespace, so a reader of the repository sees them and an admin there manages
 * them: Add a source, Check, Sync now, Rotate credential, Retire.
 * Read once when the card is opened (a folded card asks nothing); the dialogs load on
 * first use. The counts are information only: nothing waits for a person to confirm
 * a row.
 */
import { lazy, Suspense, useState } from "react";
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
import { Skeleton } from "../components/Skeleton";
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
  | { kind: "ready"; sources: KnowledgeSource[]; enabled: boolean }
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

export function RepoSourcesList({
  org,
  docsNamespace,
  repo,
  manageable,
  busy,
}: {
  org: string;
  docsNamespace: string;
  repo: string;
  /** The caller may call the source ops on the docs namespace: every verb. */
  manageable: boolean;
  busy: boolean;
}) {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [nonce, setNonce] = useState(0);
  const [adding, setAdding] = useState(false);
  const [acting, setActing] = useState<{ verb: Verb; source: KnowledgeSource } | null>(null);
  // Details is a read: any reader of the repository may open it.
  const [details, setDetails] = useState<string | null>(null);
  const id = { org, namespace: docsNamespace };
  const reload = () => setNonce((n) => n + 1);

  useAbortableEffect(
    async (signal, live) => {
      try {
        const r = await listKnowledgeSources(id, { signal });
        if (live()) setState({ kind: "ready", sources: r.sources, enabled: r.enabled });
      } catch (e) {
        if (!live()) return;
        if (isOpAbsent(e)) setState({ kind: "absent" });
        else if (e instanceof ApiError && (e.status === 403 || e.status === 401)) setState({ kind: "refused" });
        else setState({ kind: "error", message: humanizeError(e) });
      }
    },
    [org, docsNamespace, nonce],
  );

  const menu = (s: KnowledgeSource): RowMenuItem[] => {
    if (!manageable) return [];
    const act = (verb: Verb) => () => setActing({ verb, source: s });
    const items: RowMenuItem[] = [
      { label: "Check", icon: "check", onSelect: act("check") },
      { label: "Sync now", icon: "refresh", onSelect: act("sync") },
      { label: "Rotate credential", icon: "key", onSelect: act("rotate") },
      { label: "Retire", icon: "trash", danger: true, onSelect: act("retire") },
    ];
    return items.map((item) => ({ ...item, disabled: busy }));
  };
  const close = (v: boolean) => {
    if (!v) setActing(null);
  };

  return (
    <div className="space-y-1.5">
      {state.kind === "loading" && (
        <LoadingSkeleton label="Loading the repository's document sources">
          <Skeleton className="h-4 w-1/2" />
        </LoadingSkeleton>
      )}
      {state.kind === "absent" && (
        <p className="text-xs text-text-muted">Document sources are not available on this deployment yet.</p>
      )}
      {state.kind === "refused" && (
        <p className="text-xs text-text-muted">Join the repository to see where its documents come from.</p>
      )}
      {state.kind === "error" && (
        <p className="text-xs text-text-muted" role="status">
          Couldn't list the document sources — {state.message}
        </p>
      )}
      {state.kind === "ready" && !state.enabled && <p className="text-xs text-text-muted">{SYNC_OFF}</p>}
      {state.kind === "ready" && state.sources.length === 0 && (
        <p className="text-xs text-text-muted">
          No document sources.{" "}
          {manageable
            ? "Add a source and Loom keeps the repository's documents in step with it."
            : "Anyone with admin on the repository adds them."}
        </p>
      )}
      {state.kind === "ready" && state.sources.length > 0 && (
        <ul className="space-y-1.5" aria-label="Document sources">
          {state.sources.map((s) => (
            <SourceRow key={s.name} s={s} items={menu(s)} onOpen={() => setDetails(s.name)} />
          ))}
        </ul>
      )}
      {manageable && state.kind === "ready" && (
        <Button type="empty" htmlType="button" iconLeft="plus" disabled={busy} onClick={() => setAdding(true)}>
          Add a source
        </Button>
      )}
      {details && (
        <Suspense fallback={null}>
          <SourceDetailsDialog org={org} namespace={docsNamespace} name={details} onClose={() => setDetails(null)} />
        </Suspense>
      )}
      {adding && (
        <Suspense fallback={null}>
          <AddSourceDialog
            open
            org={org}
            namespace={docsNamespace}
            repo={repo}
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
            <CheckSourceDialog
              open
              org={org}
              namespace={docsNamespace}
              name={acting.source.name}
              onOpenChange={close}
            />
          )}
          {acting.verb === "sync" && (
            <SyncNowDialog
              open
              org={org}
              namespace={docsNamespace}
              name={acting.source.name}
              onOpenChange={close}
              onRequested={reload}
            />
          )}
          {acting.verb === "rotate" && (
            <RotateSourceDialog
              open
              org={org}
              namespace={docsNamespace}
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
              namespace={docsNamespace}
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
}
