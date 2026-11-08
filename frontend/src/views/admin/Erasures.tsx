import { useState } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Table } from "@clickhouse/click-ui/Table";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  LONG_CALL_MS,
  exportErasureScope,
  getErasureStatus,
  listErasures,
  requestErasure,
  type ErasureExport,
  type ErasureListRow,
  type ErasureScope,
  type ErasureStatus,
} from "../../api";
import { useIdentity, useStore } from "../../lib/state";
import { hasAdminClaim, hasOrgAdminClaim, hasWriterClaim } from "../../lib/role";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { useAbortableEffect } from "../../lib/hooks";
import { ApiError } from "../../lib/errors";
import { humanizeError, listTime } from "../../lib/format";
import { NameBadge } from "../../components/AgentBadge";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { Modal, ModalFooter } from "../../components/ui/Modal";
import { TableSkeleton } from "../../components/Skeleton";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { Alert, ChoiceGroup, Loading, MetaList, MetaRow, TipTarget, fieldError } from "../../components/ui";
import { saveJson } from "../../lib/download";

const ERASURE_STATE: Record<string, BadgeState> = { completed: "success", failed: "danger" };

type ScopeKind = ErasureScope["scope_type"];

/**
 * The erasure sagas of the org, and the controls to start one or export a scope's data.
 * erasure.list scopes the rows by caller (an org admin sees every one; anyone else the
 * namespace-scoped erasures of the namespaces they reach). Each scope is offered by the
 * rule its handler applies: erasing a namespace needs admin there (a grant counts) and a
 * flat claim of writer or above (the op is authorized for the org before the handler
 * reads the grant), exporting one an admin claim (the op is authorized for the org, where
 * only a claim reaches), and either for one user across the org an org admin claim.
 */
export function ErasuresPanel() {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const [rows, setRows] = useState<ErasureListRow[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  const [dialog, setDialog] = useState<"request" | "export" | null>(null);
  const [details, setDetails] = useState<string | null>(null);

  const orgWide: ScopeKind[] = hasOrgAdminClaim(whoami) ? ["user_id"] : [];
  const eraseScopes: ScopeKind[] = [
    ...(role === "admin" && hasWriterClaim(whoami) ? ["namespace" as const] : []),
    ...orgWide,
  ];
  const exportScopes: ScopeKind[] = [...(hasAdminClaim(whoami) ? ["namespace" as const] : []), ...orgWide];

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setRows(null);
      listErasures({ org: identity.org }, { limit: 100, signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e) => {
          if (live()) setError(e);
        });
    },
    [identity.org, reload],
  );

  const actions = (eraseScopes.length > 0 || exportScopes.length > 0) && (
    <div className="mb-4 flex flex-wrap justify-end gap-2">
      {exportScopes.length > 0 && (
        <Button type="secondary" htmlType="button" onClick={() => setDialog("export")}>
          Export data
        </Button>
      )}
      {eraseScopes.length > 0 && (
        <Button type="danger" htmlType="button" onClick={() => setDialog("request")}>
          Erase data
        </Button>
      )}
    </div>
  );

  let body: React.ReactNode;
  if (error) {
    body = <ListState kind="error" things="erasures" error={error} onRetry={() => setReload((n) => n + 1)} />;
  } else if (rows !== null && rows.length === 0) {
    body = (
      <ListState
        kind="empty"
        icon="trash"
        title="No erasures yet"
        description="An erasure removes everything Loom holds for one namespace or one user, across every table and external backend, and lands here with its status. Single memories deleted by hand are under Memories → Recently deleted."
      />
    );
  } else {
    body = <ErasuresTable rows={rows} onDetails={setDetails} />;
  }

  return (
    <>
      {actions}
      {body}
      {dialog && (
        <ScopeDialog
          mode={dialog}
          scopes={dialog === "request" ? eraseScopes : exportScopes}
          onClose={(changed) => {
            setDialog(null);
            if (changed) setReload((n) => n + 1);
          }}
        />
      )}
      {details && <ErasureDetails erasureId={details} onClose={() => setDetails(null)} />}
    </>
  );
}

function ErasuresTable({ rows, onDetails }: { rows: ErasureListRow[] | null; onDetails: (id: string) => void }) {
  return (
    <Table
      aria-label="Erasures"
      aria-busy={rows === null || undefined}
      noDataMessage={rows === null ? <TableSkeleton label="Loading erasures" /> : undefined}
      size="sm"
      headers={[
        { label: "Scope" },
        { label: "Requester" },
        { label: "Status" },
        { label: "Steps", width: "80px" },
        { label: "Requested", width: "120px" },
        { label: "Completed", width: "120px" },
        { label: <span className="sr-only">Actions</span>, width: "96px" },
      ]}
      rows={(rows ?? []).map((r) => ({
        id: r.erasure_id,
        items: [
          {
            label: (
              <span className="font-mono text-xs">
                <span className="text-text-muted">{r.scope_type}=</span>
                {r.scope_value}
              </span>
            ),
          },
          { label: <NameBadge name={r.requester} /> },
          {
            label: (
              <span className="inline-flex items-center gap-2">
                <Badge text={r.status} state={ERASURE_STATE[r.status] ?? "neutral"} size="sm" />
                {r.reversals_count > 0 && (
                  <TipTarget
                    tip={`${r.reversals_count} memor${r.reversals_count === 1 ? "y" : "ies"} restored via memory.restore — the "completed" status overstates what's actually erased.`}
                  >
                    <Badge text={`${r.reversals_count} reversed`} state="warning" size="sm" />
                  </TipTarget>
                )}
              </span>
            ),
          },
          { label: <span className="tabular">{r.steps_completed_count}</span> },
          { label: <ListTime time={r.requested_at} quiet /> },
          { label: r.completed_at ? <ListTime time={r.completed_at} quiet /> : "—" },
          {
            label: (
              <Button
                type="empty"
                htmlType="button"
                aria-label={`Details of the erasure of ${r.scope_type}=${r.scope_value} requested ${listTime(r.requested_at)}`}
                onClick={() => onDetails(r.erasure_id)}
              >
                Details
              </Button>
            ),
          },
        ],
      }))}
    />
  );
}

const SCOPE_LABEL: Record<ScopeKind, string> = {
  namespace: "This namespace",
  user_id: "One user, across the org",
};

/** The erase and export dialogs: the same scope choice, then either the saga (after a
 *  confirmation) or the export, saved as a JSON file. */
function ScopeDialog({
  mode,
  scopes,
  onClose,
}: {
  mode: "request" | "export";
  scopes: ScopeKind[];
  onClose: (changed: boolean) => void;
}) {
  const identity = useIdentity();
  const namespace = identity.namespace;
  const requester = useStore((s) => s.whoami?.subject) || "dashboard";
  const [kind, setKind] = useState<ScopeKind>(scopes[0]);
  const [userId, setUserId] = useState("");
  const [purge, setPurge] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const user = userId.trim();
  const userError = kind === "user_id" && userId !== "" && ["-", "*"].includes(user) ? "Name one user" : null;
  const scope: ErasureScope = { scope_type: kind, scope_value: kind === "namespace" ? namespace : user };
  const ready = scope.scope_value !== "" && !userError && !busy;
  const target = kind === "namespace" ? `namespace ${namespace}` : `user ${user}`;
  const erase = mode === "request";
  // Only a user's erasure purges; a namespace's tombstones whatever is asked.
  const purges = erase && kind === "user_id" && purge;

  const submit = async () => {
    if (!ready) return;
    if (erase) {
      const ok = await confirmAction({
        title: `Erase everything for ${target}?`,
        message: purges
          ? "Every row is hard-deleted, in every table and external backend. Nothing can be restored."
          : "Memories can be restored for 14 days from Memories → Recently deleted; everything else, external backends included, is gone.",
        confirmLabel: "Erase",
        danger: true,
      });
      if (!ok) return;
    }
    setBusy(true);
    setError(null);
    const signal = AbortSignal.timeout(LONG_CALL_MS);
    try {
      if (erase) {
        const r = await requestErasure(identity.org, scope, requester, purges, signal);
        toast(
          r.status === "failed" ? `Erasure failed: ${r.failure_reason}` : `Erasure ${r.status}`,
          r.status === "failed" ? "error" : "ok",
        );
        onClose(true);
      } else {
        const data = await exportErasureScope(identity.org, scope, requester, SECTION_LIMIT, signal);
        saveJson(data, `loom-export-${scope.scope_value}.json`);
        const cut = truncatedSections(data);
        if (cut.length) toast(`The export stopped at its limit in ${cut.join(", ")}`, "info");
        onClose(false);
      }
    } catch (e) {
      // Only a 4xx is a refusal. Without an answer, or after a 5xx (Loom's own may come
      // after the saga was registered), the saga may still be running, and a second Erase
      // would start another.
      if (erase && !(e instanceof ApiError && e.status < 500)) {
        toast("The erasure may still be running; it shows below as in progress until it ends", "info");
        onClose(true);
        return;
      }
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onOpenChange={(v) => !v && !busy && onClose(false)}
      title={erase ? "Erase data" : "Export data"}
      className="w-full max-w-[520px]"
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-4">
          <p className="text-sm text-text-muted leading-relaxed">
            {erase
              ? "Removes every memory, document, conversation and event Loom holds for the scope, then checks that none is left."
              : `Downloads the memories, documents, conversations and events Loom holds for the scope as one JSON file, up to ${SECTION_LIMIT.toLocaleString()} rows a section; conversations also stop at a size limit.`}
          </p>
          {scopes.length > 1 && (
            <ChoiceGroup
              label="Scope"
              value={kind}
              onChange={setKind}
              options={scopes.map((s) => ({ value: s, label: SCOPE_LABEL[s] }))}
            />
          )}
          {kind === "namespace" ? (
            <MetaList>
              <MetaRow label="Namespace">
                <span className="font-mono">{namespace}</span>
              </MetaRow>
            </MetaList>
          ) : (
            <TextField
              id="erasure-user-id"
              label="User id"
              value={userId}
              onChange={setUserId}
              {...fieldError("erasure-user-id-error", userError)}
            />
          )}
          {erase && kind === "user_id" && (
            <Checkbox
              label="Purge: hard-delete with no restore window"
              checked={purge}
              onCheckedChange={(c) => setPurge(c === true)}
            />
          )}
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" disabled={busy} onClick={() => onClose(false)}>
            Cancel
          </Button>
          <Button type={erase ? "danger" : "primary"} htmlType="submit" disabled={!ready} loading={busy}>
            {erase ? "Erase" : "Download"}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}

// erasure.export's default `section_limit`.
const SECTION_LIMIT = 10_000;

const SECTIONS = ["memory", "knowledge", "conversations", "conversation_threads", "agent_events", "audit_log"] as const;

/** The sections the export cut at their limit. */
function truncatedSections(data: ErasureExport): string[] {
  return SECTIONS.filter((s) => data[s].truncated).map((s) => s.replaceAll("_", " "));
}

/** One saga's state from erasure.status: where it stands and what the final census found. */
function ErasureDetails({ erasureId, onClose }: { erasureId: string; onClose: () => void }) {
  const [status, setStatus] = useState<ErasureStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  useAbortableEffect(
    (signal, live) => {
      getErasureStatus(erasureId, { signal })
        .then((s) => live() && setStatus(s))
        .catch((e) => live() && setError(e));
    },
    [erasureId],
  );
  const residue = Object.entries(status?.residue ?? {}).filter(([, n]) => n > 0);
  return (
    <Modal open onOpenChange={(v) => !v && onClose()} title="Erasure details" className="w-full max-w-[560px]">
      {!!error && <Alert role="alert" state="danger" text={humanizeError(error)} />}
      {!error && status === null && <Loading label="Loading erasure" />}
      {status && !status.found && <Alert state="warning" text="This erasure is no longer recorded." />}
      {status?.found && (
        <MetaList>
          <MetaRow label="Scope">
            <span className="font-mono text-xs">
              {status.scope_type}={status.scope_value}
            </span>
          </MetaRow>
          <MetaRow label="Status">
            <Badge
              text={status.status ?? "unknown"}
              state={ERASURE_STATE[status.status ?? ""] ?? "neutral"}
              size="sm"
            />
          </MetaRow>
          <MetaRow label="Requester">{status.requester && <NameBadge name={status.requester} />}</MetaRow>
          {status.requested_at && (
            <MetaRow label="Requested">
              <ListTime time={status.requested_at} />
            </MetaRow>
          )}
          {status.completed_at && (
            <MetaRow label="Completed">
              <ListTime time={status.completed_at} />
            </MetaRow>
          )}
          {status.verified_at && (
            <MetaRow label="Verified">
              <ListTime time={status.verified_at} />
            </MetaRow>
          )}
          {status.failure_reason && <MetaRow label="Failure">{status.failure_reason}</MetaRow>}
          <MetaRow label="Left behind">
            {!status.verified_at
              ? "Not checked yet"
              : residue.length === 0
                ? "Nothing"
                : residue.map(([table, n]) => (
                    <div key={table} className="font-mono text-xs">
                      {table}: {n}
                    </div>
                  ))}
          </MetaRow>
        </MetaList>
      )}
      <ModalFooter>
        <Button type="secondary" htmlType="button" onClick={onClose}>
          Close
        </Button>
      </ModalFooter>
    </Modal>
  );
}
