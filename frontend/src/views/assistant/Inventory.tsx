import { fmtInt } from "../../lib/format";
import { Table } from "@clickhouse/click-ui/Table";
import type { InventoryPopulationName, InventoryPlane, NamespaceInventory } from "../../api";
import { AgentBadge, NameBadge } from "../../components/AgentBadge";
import { ListTime } from "../../components/ui/ListTime";

const HEADERS = [{ label: "Plane" }, { label: "Live items" }, { label: "Newest write" }, { label: "Counted" }];

const PLANE_LABEL: Record<InventoryPlane, string> = {
  memories: "Memories",
  records: "Records",
  documents: "Documents",
  runs: "Runs",
  integrations: "Integrations",
};

/** "What is in this project?": live counts and the newest write per plane, with the
 *  sections that were not counted said so, and whether a model and an embedder serve it. */
export function InventorySections({ inventory }: { inventory: NamespaceInventory }) {
  return (
    <div className="space-y-3" data-testid="assistant-inventory">
      <Table
        aria-label="What this namespace holds"
        size="sm"
        headers={HEADERS}
        rows={inventory.sections.map((s) => ({
          id: s.plane,
          items: [
            { label: PLANE_LABEL[s.plane] ?? s.plane },
            { label: <span className="tabular">{s.count === null ? "—" : fmtInt(s.count)}</span> },
            { label: s.latest_at ? <ListTime time={s.latest_at} /> : <span className="text-text-muted">—</span> },
            {
              label: s.complete ? (
                "Yes"
              ) : (
                <span data-testid="inventory-incomplete">No: {s.reason || "not counted"}</span>
              ),
            },
          ],
        }))}
      />
      <p className="text-xs text-text-muted">
        {inventory.capabilities
          .map((c) => `${c.name === "llm" ? "Model" : "Embedder"}: ${c.enabled ? "enabled" : "not enabled"}`)
          .join(" · ")}
      </p>
    </div>
  );
}

const POPULATION_LABEL: Record<InventoryPopulationName, { title: string; what: string }> = {
  members: {
    title: "Members",
    what: "people holding an active grant on this namespace (for a collection, also on a collection above it)",
  },
  agents: { title: "Agents", what: "agents observed on live memory rows" },
  authors: { title: "Authors", what: "principals the server recorded writing live memory rows" },
  subjects: { title: "Subjects", what: "users the live memory rows are filed under" },
};

/** "Who are the users in this project?": the members and the identities observed on live
 *  rows, as separate populations. Observed identities are not a directory. */
export function InventoryPopulations({ inventory }: { inventory: NamespaceInventory }) {
  return (
    <ul className="space-y-3" data-testid="assistant-populations">
      {inventory.populations.map((p) => {
        const label = POPULATION_LABEL[p.population] ?? { title: p.population, what: "" };
        return (
          <li key={p.population} data-population={p.population}>
            <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
              <span className="font-medium text-text">{label.title}</span>
              <span className="tabular text-text">{p.total === null ? "—" : fmtInt(p.total)}</span>
              <span className="text-xs text-text-muted">{label.what}</span>
            </div>
            {p.total !== null && p.top.length > 0 && p.total > p.top.length && (
              <p className="text-xs text-text-muted">The {fmtInt(p.top.length)} with the most rows are shown.</p>
            )}
            {!p.complete && <p className="text-xs text-text-muted">Not complete: {p.reason || "not counted"}</p>}
            {p.top.length > 0 && (
              <ul className="mt-1 flex flex-wrap gap-1.5" aria-label={`${label.title}, most rows first`}>
                {p.top.map((who) => (
                  <li key={who.name} className="inline-flex items-center gap-1 text-xs text-text-muted tabular">
                    {p.population === "agents" ? <AgentBadge agent={who.name} /> : <NameBadge name={who.name} />}
                    {fmtInt(who.rows)}
                  </li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** The boundary the counts were read under, when a token pinned to one user narrows it
 *  (server/identity/app/namespace_inventory.py): memories, runs and the identities observed on
 *  rows are bound to that user; documents and members stay the namespace's. The page sends no
 *  session id, so a session-bound read never reaches here. */
export function InventoryScopeNote({ inventory }: { inventory: NamespaceInventory }) {
  const { read_scope, user_id } = inventory.scope;
  if (read_scope === "namespace") return null;
  return (
    <p className="mb-2 text-xs text-text-muted" data-testid="inventory-scope">
      Memories, runs and the agents, authors and subjects on rows are counted for user{" "}
      <span className="font-mono break-all">{user_id}</span> only, as your token reads; documents and members are the
      namespace's.
    </p>
  );
}
