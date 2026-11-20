import { useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import {
  collapseMemoryDuplicates,
  consolidateMemories,
  listDocumentDuplicates,
  listMemoryAgents,
  listMemoryDuplicates,
  repairOrphanDuplicates,
  resolveMemoryDuplicate,
  retireDocumentDuplicate,
  COLLAPSE_PAIRS,
  CONSOLIDATE_READ,
  DUPLICATE_LIST_LIMIT,
  type Scope,
  type DocumentDuplicateCluster,
  type MemoryDuplicateCluster,
  type MemoryDuplicateDecision,
} from "../../api";
import { useIdentity } from "../../lib/state";
import { useAbortableEffect } from "../../lib/hooks";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast, toastError } from "../../components/Toast";
import { ListState } from "../../components/ui/ListState";
import { RowMenu } from "../../components/ui/RowMenu";
import { Loading, Panel, SectionHeading } from "../../components/ui";

/**
 * Duplicates — the pairs the dedup worker flagged, memories and documents, each settled
 * by hand, and the namespace-wide repairs: mark the search rows of duplicates so search
 * skips them, bring back duplicates whose canonical is gone, and merge one agent's
 * near-duplicates with a model call per cluster.
 */
export function DuplicatesPanel() {
  const identity = useIdentity();
  const scope = { org: identity.org, namespace: identity.namespace };
  const [reload, setReload] = useState(0);
  const refresh = () => setReload((n) => n + 1);
  return (
    <div className="space-y-6">
      <Repairs scope={scope} onDone={refresh} />
      <MemoryDuplicates scope={scope} reload={reload} onDone={refresh} />
      <DocumentDuplicates scope={scope} reload={reload} onDone={refresh} />
    </div>
  );
}

/** One list's read: its rows, its own error, and whether a refresh is out. A refresh keeps
 *  the rows on screen. */
function useDuplicateList<T>(scope: Scope, reload: number, read: (signal: AbortSignal) => Promise<T[]>) {
  const [rows, setRows] = useState<T[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setBusy(true);
      read(signal)
        .then((r) => live() && setRows(r))
        .catch((e) => live() && setError(e))
        .finally(() => live() && setBusy(false));
    },
    [scope.org, scope.namespace, reload, retry],
  );
  return { rows, error, busy, retry: () => setRetry((n) => n + 1) };
}

/** Says the list stops at the server's cap. */
function CapNote({ shown, things }: { shown: number; things: string }) {
  if (shown < DUPLICATE_LIST_LIMIT) return null;
  return (
    <p className="mt-2 text-xs text-text-muted">
      The largest {DUPLICATE_LIST_LIMIT} {things}; settle some to see the rest.
    </p>
  );
}

function MemoryDuplicates({ scope, reload, onDone }: { scope: Scope; reload: number; onDone: () => void }) {
  const list = useDuplicateList(scope, reload, (signal) =>
    listMemoryDuplicates(scope, { signal }).then((r) => r.clusters ?? []),
  );
  const section = useRef<HTMLElement>(null);
  const settled = () => {
    // The row that was decided unmounts; focus goes to the section.
    section.current?.focus();
    onDone();
  };
  return (
    <section ref={section} tabIndex={-1} className="outline-none" aria-busy={list.busy || undefined}>
      <SectionHeading label="Duplicate memories" level={2} />
      {list.error ? (
        <ListState kind="error" things="duplicate memories" error={list.error} onRetry={list.retry} />
      ) : list.rows === null ? (
        <Loading label="Loading duplicate memories" />
      ) : list.rows.length === 0 ? (
        <p className="text-sm text-text-muted">No pair waits for a decision.</p>
      ) : (
        <>
          <ul className="space-y-3">
            {list.rows.map((c) => (
              <MemoryCluster key={c.canonical_id} scope={scope} cluster={c} held={list.busy} onDone={settled} />
            ))}
          </ul>
          <CapNote shown={list.rows.length} things="clusters" />
        </>
      )}
    </section>
  );
}

function MemoryCluster({
  scope,
  cluster,
  held,
  onDone,
}: {
  scope: Scope;
  cluster: MemoryDuplicateCluster;
  /** The list is being read again: a decided pair's buttons stay off until it answers. */
  held: boolean;
  onDone: () => void;
}) {
  const [working, setWorking] = useState(false);
  const busy = working || held;
  // The canonical was deleted: its duplicates point at nothing and no pair can be settled.
  if (!cluster.canonical_key) {
    return (
      <li>
        <Panel>
          <p className="text-sm">
            {cluster.dup_count} duplicate{cluster.dup_count === 1 ? "" : "s"} of a deleted memory
            {cluster.dup_count > cluster.duplicates.length && `, ${cluster.duplicates.length} listed`}:{" "}
            <span className="font-mono text-xs">{cluster.duplicates.map((d) => d.memory_key).join(", ")}</span>
          </p>
          <p className="mt-1 text-xs text-text-muted">Repair orphaned duplicates brings them back.</p>
        </Panel>
      </li>
    );
  }

  const others = cluster.dup_count - 1;
  const decide = async (duplicateId: string, duplicateKey: string, decision: MemoryDuplicateDecision) => {
    // Keeping one of the two deletes the other; it can be restored for 14 days.
    if (decision !== "keep_both") {
      const keepsDuplicate = decision === "keep_duplicate";
      const orphaned =
        keepsDuplicate && others > 0
          ? ` ${others === 1 ? "The other duplicate of it loses its" : `The other ${others} duplicates of it lose their`} canonical; Repair orphaned duplicates brings them back.`
          : "";
      const ok = await confirmAction({
        title: `Delete ${keepsDuplicate ? cluster.canonical_key : duplicateKey}?`,
        message: `It can be restored for 14 days from Memories → Recently deleted.${orphaned}`,
        confirmLabel: "Delete",
        danger: true,
      });
      if (!ok) return;
    }
    setWorking(true);
    try {
      await resolveMemoryDuplicate(scope, cluster, duplicateId, decision);
      toast("Resolved", "ok");
      onDone();
    } catch (e) {
      toastError("Resolve failed", e);
    } finally {
      setWorking(false);
    }
  };
  return (
    <li>
      <Panel>
        <p className="text-xs text-text-muted">
          Canonical <span className="font-mono text-text">{cluster.canonical_key}</span> · {cluster.canonical_agent}
          {cluster.dup_count > cluster.duplicates.length &&
            ` · ${cluster.duplicates.length} of its ${cluster.dup_count} duplicates shown`}
        </p>
        <ul className="mt-2 space-y-2">
          {cluster.duplicates.map((d) => (
            <li key={d.memory_id} className="flex items-center gap-2">
              <span className="font-mono text-xs min-w-0 flex-1 truncate">{d.memory_key}</span>
              <span className="text-xs text-text-muted">{d.agent}</span>
              <Button
                type="secondary"
                htmlType="button"
                disabled={busy}
                aria-label={`Keep both: ${d.memory_key}`}
                onClick={() => void decide(d.memory_id, d.memory_key, "keep_both")}
              >
                Keep both
              </Button>
              <RowMenu
                name={d.memory_key}
                items={[
                  {
                    label: `Keep this one, delete ${cluster.canonical_key}`,
                    disabled: busy,
                    danger: true,
                    onSelect: () => void decide(d.memory_id, d.memory_key, "keep_duplicate"),
                  },
                  {
                    label: `Keep ${cluster.canonical_key}, delete this one`,
                    disabled: busy,
                    danger: true,
                    onSelect: () => void decide(d.memory_id, d.memory_key, "keep_canonical"),
                  },
                ]}
              />
            </li>
          ))}
        </ul>
      </Panel>
    </li>
  );
}

function DocumentDuplicates({ scope, reload, onDone }: { scope: Scope; reload: number; onDone: () => void }) {
  const list = useDuplicateList(scope, reload, (signal) =>
    listDocumentDuplicates(scope, { signal }).then((r) => r.doc_clusters ?? []),
  );
  const section = useRef<HTMLElement>(null);
  const settled = () => {
    section.current?.focus();
    onDone();
  };
  return (
    <section ref={section} tabIndex={-1} className="outline-none" aria-busy={list.busy || undefined}>
      <SectionHeading label="Duplicate documents" level={2} />
      {list.error ? (
        <ListState kind="error" things="duplicate documents" error={list.error} onRetry={list.retry} />
      ) : list.rows === null ? (
        <Loading label="Loading duplicate documents" />
      ) : list.rows.length === 0 ? (
        <p className="text-sm text-text-muted">No two documents hold the same content.</p>
      ) : (
        <>
          <ul className="space-y-3">
            {list.rows.map((c) => (
              <DocumentCluster key={c.content_sha256} scope={scope} cluster={c} held={list.busy} onDone={settled} />
            ))}
          </ul>
          <CapNote shown={list.rows.length} things="clusters" />
        </>
      )}
    </section>
  );
}

/** The oldest document is kept, and each other address is listed once: a copy is retired by
 *  its address, so one at the kept document's address would retire the kept one. */
export function keptAndCopies(docs: DocumentDuplicateCluster["docs"]) {
  const [keep, ...rest] = [...docs].sort((a, b) => a.ingested_at.localeCompare(b.ingested_at));
  const copies = new Map<string, (typeof docs)[number]>();
  for (const d of rest) if (d.source_uri !== keep.source_uri && !copies.has(d.source_uri)) copies.set(d.source_uri, d);
  return { keep, copies: [...copies.values()] };
}

function DocumentCluster({
  scope,
  cluster,
  held,
  onDone,
}: {
  scope: Scope;
  cluster: DocumentDuplicateCluster;
  held: boolean;
  onDone: () => void;
}) {
  const { keep, copies } = keptAndCopies(cluster.docs);
  const [working, setWorking] = useState(false);
  const busy = working || held;
  const retire = async (uri: string) => {
    const ok = await confirmAction({
      title: `Retire ${uri}?`,
      message: `It is superseded by ${keep.source_uri}, the oldest copy, and leaves search.`,
      confirmLabel: "Retire",
      danger: true,
    });
    if (!ok) return;
    setWorking(true);
    try {
      await retireDocumentDuplicate(scope, keep.source_uri, uri);
      toast("Copy retired", "ok");
      onDone();
    } catch (e) {
      toastError("Retire failed", e);
    } finally {
      setWorking(false);
    }
  };
  return (
    <li>
      <Panel>
        <p className="text-xs text-text-muted">
          Kept <span className="font-mono text-text">{keep.title || keep.source_uri}</span>
          {cluster.doc_count > cluster.docs.length &&
            `, the oldest of the ${cluster.docs.length} of its ${cluster.doc_count} documents listed`}
        </p>
        {copies.length === 0 && (
          <p className="mt-2 text-xs text-text-muted">
            Every listed copy has the kept document's address: nothing to retire.
          </p>
        )}
        {copies.length > 0 && (
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
        )}
      </Panel>
    </li>
  );
}

type Question = { title: string; message?: string; confirmLabel?: string; danger?: boolean };

/** The namespace-wide repairs. Each asks before it writes. */
function Repairs({ scope, onDone }: { scope: Scope; onDone: () => void }) {
  const [agents, setAgents] = useState<string[]>([]);
  const [agentsFailed, setAgentsFailed] = useState(false);
  const [agent, setAgent] = useState("");
  const [busy, setBusy] = useState(false);
  useAbortableEffect(
    (signal, live) => {
      setAgentsFailed(false);
      listMemoryAgents(scope, { signal })
        .then((a) => live() && setAgents(a.filter((x) => x !== "-")))
        .catch(() => {
          if (!live()) return;
          setAgents([]);
          setAgentsFailed(true);
        });
    },
    [scope.org, scope.namespace],
  );

  /** `ask` answers the confirmation to show, or null after saying there is nothing to do. */
  const run = async (ask: () => Promise<Question | null>, apply: () => Promise<string>) => {
    setBusy(true);
    try {
      const question = await ask();
      if (question === null) return;
      if (!(await confirmAction({ confirmLabel: "Run", ...question }))) return;
      toast(await apply(), "ok");
      onDone();
    } catch (e) {
      toastError("Failed", e);
    } finally {
      setBusy(false);
    }
  };
  const nothingToDo = (message: string) => {
    toast(message, "info");
    return null;
  };

  const restamp = () =>
    run(
      async () => {
        const r = await collapseMemoryDuplicates(scope, true);
        // The op reads the same first pairs every call: past them, nothing is known.
        const among = r.truncated ? ` among the first ${COLLAPSE_PAIRS.toLocaleString()} duplicate pairs` : "";
        if (r.mirror_stale === 0)
          return nothingToDo(
            r.truncated ? `No search row needs marking${among}.` : "Search already skips every duplicate.",
          );
        return {
          title: `Mark ${r.mirror_stale} search row${r.mirror_stale === 1 ? "" : "s"} of duplicates${among} so search skips them?`,
        };
      },
      async () => {
        const r = await collapseMemoryDuplicates(scope, false);
        return `Marked ${r.restamped} search row${r.restamped === 1 ? "" : "s"}.`;
      },
    );
  const repair = () =>
    run(
      async () => {
        const r = await repairOrphanDuplicates(scope, true);
        if (r.candidates === 0) return nothingToDo("No duplicate has lost its canonical.");
        return {
          title: `Bring back ${r.candidates} duplicate${r.candidates === 1 ? "" : "s"} whose canonical is gone?`,
        };
      },
      async () => {
        const n = (await repairOrphanDuplicates(scope, false)).reactivated;
        return `Brought back ${n} duplicate${n === 1 ? "" : "s"}.`;
      },
    );
  // No dry run first: it makes the same model calls and only skips the write.
  const consolidate = () =>
    run(
      async () => ({
        title: `Merge near-duplicates among ${agent}'s newest ${CONSOLIDATE_READ} memories?`,
        message:
          "Memories written in the last hour are left out. Each cluster found becomes one merged memory, and the memories merged into it are deleted. Each cluster costs one model call.",
        confirmLabel: "Merge",
        danger: true,
      }),
      async () => {
        const r = await consolidateMemories(scope, agent);
        if (r.clusters_found === 0) return `Read ${r.examined} of ${agent}'s memories: no near-duplicates.`;
        const kept = r.clusters_failed;
        const why = r.clusters_refused_lossy ? ` (${r.clusters_refused_lossy} because the merge lost a detail)` : "";
        const failed = kept ? `; ${kept} left as ${kept === 1 ? "it was" : "they were"}${why}` : "";
        return `Merged ${r.consolidated.length} of ${r.clusters_found} clusters${failed}.`;
      },
    );

  return (
    <Panel>
      <SectionHeading label="Repairs" level={2} />
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void restamp()}>
          Hide duplicates from search
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
            placeholder={
              agentsFailed
                ? "Couldn't load the agents"
                : agents.length === 0
                  ? "No agent has written here"
                  : "Pick an agent"
            }
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
      <p className="mt-2 text-xs text-text-muted">Merging reads the agent's memories that belong to no user.</p>
    </Panel>
  );
}
