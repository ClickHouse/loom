import { useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Table } from "@clickhouse/click-ui/Table";
import { decideProposals, listProposals, type MemoryProposal } from "../../api";
import { useIdentity, useStore } from "../../lib/state";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { canCall } from "../../lib/canCall";
import { useAbortableEffect } from "../../lib/hooks";
import { humanizeError } from "../../lib/format";
import { toast } from "../../components/Toast";
import { AgentBadge } from "../../components/AgentBadge";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { RowMenu } from "../../components/ui/RowMenu";
import { TableSkeleton } from "../../components/Skeleton";

const HEADERS = [
  { label: "Fact" },
  { label: "Agent", width: "160px" },
  { label: "Graded runs", width: "200px" },
  { label: "Written", width: "110px" },
  { label: <span className="sr-only">Decision</span>, width: "150px" },
];

/** Facts on trial (written with `proposed=true`) and how the runs that relied on each
 *  went: accept one to serve it by default, reject it to retire it. */
export function ProposalsPanel({ initial }: { initial: { rows: MemoryProposal[]; total: number } | null }) {
  const identity = useIdentity();
  const scope = { org: identity.org, namespace: identity.namespace };
  const [rows, setRows] = useState<MemoryProposal[] | null>(initial?.rows ?? null);
  const [total, setTotal] = useState(initial?.total ?? 0);
  const panel = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const canDecide = canCall("memory.proposal_decide", role, whoami);

  useAbortableEffect(
    (signal, live) => {
      if (reload === 0 && initial) return;
      setError(null);
      listProposals(scope, { signal })
        .then((r) => {
          if (!live()) return;
          setRows(r.proposals ?? []);
          setTotal(r.total ?? 0);
        })
        .catch((e) => live() && setError(e));
    },
    [identity.org, identity.namespace, reload],
  );

  const decide = async (key: string, decision: "accept" | "reject") => {
    setBusy(true);
    try {
      const r = await decideProposals(scope, [key], decision);
      if (r.decided > 0) toast(decision === "accept" ? "Accepted" : "Rejected", "ok");
      // Not a proposal any more: decided by the policy or by someone else first.
      else toast(`Not decided: ${r.results[0]?.reason || "it is no longer a proposal"}`, "info");
    } catch (e) {
      toast(`Decision failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
    // The decided row unmounts; focus goes to the panel, and the list is read again.
    panel.current?.focus();
    setReload((n) => n + 1);
  };

  if (error) return <ListState kind="error" things="proposals" error={error} onRetry={() => setReload((n) => n + 1)} />;
  if (rows?.length === 0)
    return (
      <ListState
        kind="empty"
        icon="check-in-circle"
        title="No proposals"
        description="A fact an agent writes as proposed waits here until it is accepted or rejected."
      />
    );
  return (
    <div ref={panel} tabIndex={-1} className="outline-none">
      <Table
        aria-label="Proposals"
        aria-busy={rows === null || undefined}
        noDataMessage={rows === null ? <TableSkeleton label="Loading proposals" /> : undefined}
        size="sm"
        headers={HEADERS}
        rows={(rows ?? []).map((p) => ({
          id: p.memory_id,
          items: [
            {
              label: (
                <div className="min-w-0">
                  <div className="font-mono text-xs">{p.memory_key}</div>
                  {p.sensitivity === "high" ? (
                    <Badge text="sensitive" state="neutral" size="sm" />
                  ) : (
                    <div className="text-sm line-clamp-2">{p.value_preview}</div>
                  )}
                </div>
              ),
              overflowMode: "wrap",
            },
            { label: <AgentBadge agent={p.agent} /> },
            {
              label: (
                <span className="inline-flex items-center gap-1.5 tabular">
                  {p.graded_passes} passed · {p.graded_fails} failed
                  {p.ready && <Badge text={`ready to ${p.ready}`} state="info" size="sm" />}
                </span>
              ),
            },
            { label: <ListTime time={p.created_at} quiet /> },
            {
              label: canDecide && (
                <span className="inline-flex items-center gap-2">
                  <Button
                    type="secondary"
                    htmlType="button"
                    disabled={busy}
                    aria-label={`Accept ${p.memory_key}`}
                    onClick={() => void decide(p.memory_key, "accept")}
                  >
                    Accept
                  </Button>
                  <RowMenu
                    name={p.memory_key}
                    items={[
                      {
                        label: "Reject",
                        danger: true,
                        disabled: busy,
                        onSelect: () => void decide(p.memory_key, "reject"),
                      },
                    ]}
                  />
                </span>
              ),
            },
          ],
        }))}
      />
      {rows !== null && total > rows.length && (
        <p className="mt-2 text-xs text-text-muted">
          The newest {rows.length} of {total} proposals; decide some to see the rest.
        </p>
      )}
    </div>
  );
}
