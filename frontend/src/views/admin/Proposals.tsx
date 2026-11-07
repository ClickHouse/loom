import { useEffect, useRef, useState } from "react";
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
import { Tip } from "../../components/ui";
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
export function ProposalsPanel({
  initial,
  onSeedUsed,
}: {
  initial: { rows: MemoryProposal[]; total: number } | null;
  onSeedUsed: () => void;
}) {
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

  // The probe's rows seed the first view only; a panel opened again reads afresh.
  useEffect(() => {
    if (initial) onSeedUsed();
  }, []);
  // A decision holds every row until the list is read again.
  const [reading, setReading] = useState(false);
  useAbortableEffect(
    (signal, live) => {
      if (reload === 0 && initial) return;
      setError(null);
      setReading(true);
      listProposals(scope, { signal })
        .then((r) => {
          if (!live()) return;
          setRows(r.proposals);
          setTotal(r.total);
        })
        .catch((e) => live() && setError(e))
        .finally(() => live() && setReading(false));
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
    setReading(true);
    panel.current?.focus();
    setReload((n) => n + 1);
  };
  const held = busy || reading;
  // memory.proposal_decide names a proposal by key and decides the newest row of it, so a
  // key two agents proposed cannot be decided here for one of them.
  const shared = new Set((rows ?? []).map((p) => p.memory_key).filter((k, i, all) => all.indexOf(k) !== i));

  // One wrapper in every state, so the focus a decision gives it survives an empty answer.
  return (
    <div ref={panel} tabIndex={-1} className="outline-none">
      {error ? (
        <ListState kind="error" things="proposals" error={error} onRetry={() => setReload((n) => n + 1)} />
      ) : rows?.length === 0 ? (
        <ListState
          kind="empty"
          icon="check-in-circle"
          title="No proposals"
          description="A fact an agent writes as proposed waits here until it is accepted or rejected."
        />
      ) : (
        <>
          <Table
            aria-label="Proposals"
            aria-busy={rows === null || reading || undefined}
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
                  label:
                    canDecide &&
                    (shared.has(p.memory_key) ? (
                      <Tip content="Two agents proposed this key, and a decision names only the key.">
                        <span className="text-xs text-text-muted">Decide over the API</span>
                      </Tip>
                    ) : (
                      <span className="inline-flex items-center gap-2">
                        <Button
                          type="secondary"
                          htmlType="button"
                          disabled={held}
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
                              disabled: held,
                              onSelect: () => void decide(p.memory_key, "reject"),
                            },
                          ]}
                        />
                      </span>
                    )),
                },
              ],
            }))}
          />
          {rows !== null && total > rows.length && (
            <p className="mt-2 text-xs text-text-muted">
              The newest {rows.length} proposals are listed; decide some to see the rest.
            </p>
          )}
        </>
      )}
    </div>
  );
}
