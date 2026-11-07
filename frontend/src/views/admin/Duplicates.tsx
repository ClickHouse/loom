import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import {
  collapseMemoryDuplicates,
  consolidateMemories,
  listDocumentDuplicates,
  listMemoryDuplicates,
  listWritingAgents,
  repairOrphanDuplicates,
  resolveMemoryDuplicate,
  retireDocumentDuplicate,
  type DocumentDuplicateCluster,
  type MemoryDuplicateCluster,
  type MemoryDuplicateDecision,
} from "../../api";
import { useIdentity } from "../../lib/state";
import { useAbortableEffect } from "../../lib/hooks";
import { humanizeError } from "../../lib/format";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { ListState } from "../../components/ui/ListState";
import { Loading, Panel, SectionHeading } from "../../components/ui";

type Scope = { org: string; namespace: string };

/**
 * Duplicates — the pairs the dedup worker flagged, memories and documents, each settled
 * by hand, and the namespace-wide repairs: collapse every pair onto its canonical, bring
 * back duplicates whose canonical is gone, and merge one agent's near-duplicates with a
 * model call per cluster. Every repair counts what it would do first.
 */
export function DuplicatesPanel() {
  const identity = useIdentity();
  const scope = { org: identity.org, namespace: identity.namespace };
  const [memories, setMemories] = useState<MemoryDuplicateCluster[] | null>(null);
  const [documents, setDocuments] = useState<DocumentDuplicateCluster[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      Promise.all([listMemoryDuplicates(scope, { signal }), listDocumentDuplicates(scope, { signal })])
        .then(([m, d]) => {
          if (!live()) return;
          setMemories(m.clusters ?? []);
          setDocuments(d.doc_clusters ?? []);
        })
        .catch((e) => live() && setError(e));
    },
    [identity.org, identity.namespace, reload],
  );
  const refresh = () => setReload((n) => n + 1);

  if (error) return <ListState kind="error" things="duplicates" error={error} onRetry={refresh} />;
  return (
    <div className="space-y-6">
      <Repairs scope={scope} onDone={refresh} />
      <section>
        <SectionHeading label="Duplicate memories" level={2} />
        {memories === null ? (
          <Loading label="Loading duplicate memories" />
        ) : memories.length === 0 ? (
          <p className="text-sm text-text-muted">No pair waits for a decision.</p>
        ) : (
          <ul className="space-y-3">
            {memories.map((c) => (
              <MemoryCluster key={c.canonical_id} scope={scope} cluster={c} onDone={refresh} />
            ))}
          </ul>
        )}
      </section>
      <section>
        <SectionHeading label="Duplicate documents" level={2} />
        {documents === null ? (
          <Loading label="Loading duplicate documents" />
        ) : documents.length === 0 ? (
          <p className="text-sm text-text-muted">No two documents hold the same content.</p>
        ) : (
          <ul className="space-y-3">
            {documents.map((c) => (
              <DocumentCluster key={c.content_sha256} scope={scope} cluster={c} onDone={refresh} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

const MEMORY_DECISIONS: { decision: MemoryDuplicateDecision; label: string }[] = [
  { decision: "keep_both", label: "Keep both" },
  { decision: "keep_duplicate", label: "Keep this one" },
  { decision: "keep_canonical", label: "Drop this one" },
];

function MemoryCluster({
  scope,
  cluster,
  onDone,
}: {
  scope: Scope;
  cluster: MemoryDuplicateCluster;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const decide = async (duplicateId: string, duplicateKey: string, decision: MemoryDuplicateDecision) => {
    // Keeping one of the two tombstones the other; it can be restored for 14 days.
    if (decision !== "keep_both") {
      const dropped = decision === "keep_canonical" ? duplicateKey : cluster.canonical_key;
      const ok = await confirmAction({
        title: `Delete ${dropped}?`,
        message: "It can be restored for 14 days from Memories → Recently deleted.",
        confirmLabel: "Delete",
        danger: true,
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      await resolveMemoryDuplicate(scope, cluster, duplicateId, decision);
      toast("Resolved", "ok");
      onDone();
    } catch (e) {
      toast(`Resolve failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <li>
      <Panel>
        <p className="text-xs text-text-muted">
          Canonical <span className="font-mono text-text">{cluster.canonical_key}</span> · {cluster.canonical_agent}
        </p>
        <ul className="mt-2 space-y-2">
          {cluster.duplicates.map((d) => (
            <li key={d.memory_id} className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs min-w-0 flex-1 truncate">{d.memory_key}</span>
              <span className="text-xs text-text-muted">{d.agent}</span>
              {MEMORY_DECISIONS.map((m) => (
                <Button
                  key={m.decision}
                  type={m.decision === "keep_canonical" ? "danger" : "secondary"}
                  htmlType="button"
                  disabled={busy}
                  aria-label={`${m.label}: ${d.memory_key}`}
                  onClick={() => void decide(d.memory_id, d.memory_key, m.decision)}
                >
                  {m.label}
                </Button>
              ))}
            </li>
          ))}
        </ul>
      </Panel>
    </li>
  );
}

function DocumentCluster({
  scope,
  cluster,
  onDone,
}: {
  scope: Scope;
  cluster: DocumentDuplicateCluster;
  onDone: () => void;
}) {
  const [keep, ...copies] = cluster.docs;
  const [busy, setBusy] = useState(false);
  const retire = async (uri: string) => {
    const ok = await confirmAction({
      title: `Retire ${uri}?`,
      message: `It is superseded by ${keep.source_uri} and leaves search.`,
      confirmLabel: "Retire",
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await retireDocumentDuplicate(scope, keep.source_uri, uri);
      toast("Copy retired", "ok");
      onDone();
    } catch (e) {
      toast(`Retire failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <li>
      <Panel>
        <p className="text-xs text-text-muted">
          Kept <span className="font-mono text-text">{keep.title || keep.source_uri}</span>
        </p>
        <ul className="mt-2 space-y-2">
          {copies.map((d) => (
            <li key={d.doc_id} className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs min-w-0 flex-1 truncate">{d.title || d.source_uri}</span>
              <Button
                type="danger"
                htmlType="button"
                disabled={busy}
                aria-label={`Retire copy: ${d.source_uri}`}
                onClick={() => void retire(d.source_uri)}
              >
                Retire copy
              </Button>
            </li>
          ))}
        </ul>
      </Panel>
    </li>
  );
}

/** The namespace-wide repairs. Each runs a dry run first and asks before it writes. */
function Repairs({ scope, onDone }: { scope: Scope; onDone: () => void }) {
  const [agents, setAgents] = useState<string[]>([]);
  const [agent, setAgent] = useState("");
  const [busy, setBusy] = useState(false);
  useAbortableEffect(
    (signal, live) => {
      listWritingAgents(scope, { signal })
        .then((a) => live() && setAgents(a.filter((x) => x !== "-")))
        .catch(() => live() && setAgents([]));
    },
    [scope.org, scope.namespace],
  );

  const run = async (preview: () => Promise<string | null>, apply: () => Promise<string>) => {
    setBusy(true);
    try {
      const question = await preview();
      if (question === null) return;
      if (!(await confirmAction({ title: question, confirmLabel: "Run" }))) return;
      toast(await apply(), "ok");
      onDone();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const collapse = () =>
    run(
      async () => {
        const r = await collapseMemoryDuplicates(scope, true);
        if (r.pairs === 0) return none("No duplicate pair to collapse.");
        return `Collapse ${r.pairs} duplicate pair${r.pairs === 1 ? "" : "s"} onto their canonical?`;
      },
      async () => `Collapsed ${(await collapseMemoryDuplicates(scope, false)).pairs} pairs`,
    );
  const repair = () =>
    run(
      async () => {
        const r = await repairOrphanDuplicates(scope, true);
        if (r.candidates === 0) return none("No duplicate has lost its canonical.");
        return `Bring back ${r.candidates} duplicate${r.candidates === 1 ? "" : "s"} whose canonical is gone?`;
      },
      async () => `Brought back ${(await repairOrphanDuplicates(scope, false)).reactivated}`,
    );
  const consolidate = () =>
    run(
      async () => {
        const r = await consolidateMemories(scope, agent, true);
        if (r.clusters_found === 0) return none(`No near-duplicates among ${r.examined} memories of ${agent}.`);
        return `Merge ${r.clusters_found} cluster${r.clusters_found === 1 ? "" : "s"} of ${agent}'s memories? Each merge is one model call.`;
      },
      async () => `Merged ${(await consolidateMemories(scope, agent, false)).consolidated.length} clusters`,
    );

  return (
    <Panel>
      <SectionHeading label="Repairs" level={2} />
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void collapse()}>
          Collapse all pairs
        </Button>
        <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void repair()}>
          Repair orphaned duplicates
        </Button>
        <div className="min-w-[200px]">
          <Select
            id="consolidate-agent"
            label="Agent"
            value={agent || undefined}
            onSelect={setAgent}
            placeholder={agents.length === 0 ? "No agent has written here" : "Pick an agent"}
            disabled={agents.length === 0}
          >
            {agents.map((a) => (
              <Select.Item key={a} value={a}>
                {a}
              </Select.Item>
            ))}
          </Select>
        </div>
        <Button type="secondary" htmlType="button" disabled={busy || !agent} onClick={() => void consolidate()}>
          Merge near-duplicates
        </Button>
      </div>
    </Panel>
  );
}

/** A dry run that found nothing to do: said, and no confirmation follows. */
function none(message: string): null {
  toast(message, "info");
  return null;
}
