import { Button } from "@clickhouse/click-ui/Button";
import type { InspectSubject, InventoryPlane, InventoryPopulation, NamespaceInventory } from "../../api";
import { NameBadge } from "../../components/AgentBadge";
import { Unavailable } from "../../components/ui";
import { ListTime } from "../../components/ui/ListTime";
import { displayAgent, fmtInt } from "../../lib/format";

const PLANE_LABEL: Record<InventoryPlane, string> = {
  memories: "Memories",
  records: "Records",
  documents: "Documents",
  runs: "Runs",
  integrations: "Integrations",
};

/** What the namespace holds: the live count and newest write per plane, with a plane that
 *  was not counted said so (never shown as zero), and whether a model and an embedder serve it. */
export function InventorySections({ inventory }: { inventory: NamespaceInventory }) {
  return (
    <div data-testid="assistant-inventory">
      <dl className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
        {inventory.sections.map((s) => (
          <div key={s.plane} className="rounded-md border border-border px-3 py-2 min-w-0" data-plane={s.plane}>
            <dt className="text-xs text-text-muted">{PLANE_LABEL[s.plane] ?? s.plane}</dt>
            <dd className="text-lg text-text tabular">{s.count === null ? "—" : fmtInt(s.count)}</dd>
            {s.complete ? (
              s.latest_at && (
                <dd className="text-xs text-text-muted">
                  Newest <ListTime time={s.latest_at} />
                </dd>
              )
            ) : (
              <dd className="text-xs text-text-muted break-words" data-testid="inventory-incomplete">
                Not counted: {s.reason || "no reason given"}
              </dd>
            )}
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-text-muted">
        {inventory.capabilities
          .map((c) => `${c.name === "llm" ? "Model" : "Embedder"}: ${c.enabled ? "enabled" : "not enabled"}`)
          .join(" · ")}
      </p>
    </div>
  );
}

// The population the overview lists for each subject; sessions are not listed.
export const POPULATION_OF: Record<InspectSubject, InventoryPopulation["population"] | null> = {
  user: "subjects",
  agent: "agents",
  session: null,
};

const POPULATION_LABEL: Record<InventoryPopulation["population"], { title: string; what: string }> = {
  subjects: { title: "Users", what: "users that memories are filed under" },
  agents: { title: "Agents", what: "agents that wrote memories" },
  authors: { title: "Authors", what: "people and services that wrote memories" },
  members: { title: "Members", what: "people with access to this namespace" },
};

const ORDER: InventoryPopulation["population"][] = ["subjects", "agents", "authors", "members"];

/** Who appears in the namespace. Users and agents are buttons that inspect that subject;
 *  authors and members are listed only, as no inspection is filed under them. Members are
 *  the grants; the rest are observed on rows, so they are not a directory. */
export function InventoryPopulations({
  inventory,
  onPick,
  pickBlocked,
}: {
  inventory: NamespaceInventory;
  onPick: (subject: InspectSubject, id: string) => void;
  pickBlocked: string | null;
}) {
  const populations = [...inventory.populations].sort(
    (a, b) => ORDER.indexOf(a.population) - ORDER.indexOf(b.population),
  );
  return (
    <ul className="space-y-3" data-testid="assistant-populations">
      {populations.map((p) => {
        const label = POPULATION_LABEL[p.population] ?? { title: p.population, what: "" };
        const subject = (Object.keys(POPULATION_OF) as InspectSubject[]).find((s) => POPULATION_OF[s] === p.population);
        return (
          <li key={p.population} data-population={p.population}>
            <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
              <span className="font-medium text-text">{label.title}</span>
              <span className="tabular text-text">{p.total === null ? "—" : fmtInt(p.total)}</span>
              <span className="text-xs text-text-muted">{label.what}</span>
            </div>
            {p.total !== null && p.top.length > 0 && p.total > p.top.length && (
              <p className="text-xs text-text-muted">
                The {fmtInt(p.top.length)} with the most memories are shown
                {subject ? "; type any other in the field below." : "."}
              </p>
            )}
            {!p.complete && <p className="text-xs text-text-muted">Not complete: {p.reason || "no reason given"}</p>}
            {p.top.length > 0 && (
              <ul className="mt-1.5 flex flex-wrap gap-1.5" aria-label={`${label.title}, most memories first`}>
                {p.top.map((who) => (
                  <li key={who.name} className="inline-flex items-center gap-1 text-xs text-text-muted tabular">
                    {subject ? (
                      <Unavailable reason={pickBlocked}>
                        <Button
                          type="secondary"
                          htmlType="button"
                          onClick={() => onPick(subject, who.name)}
                          data-testid={`pick-${subject}`}
                          title={who.name}
                        >
                          <span className="truncate max-w-[24ch]">
                            {subject === "agent" ? displayAgent(who.name) : who.name}
                          </span>{" "}
                          <span className="ml-1.5 text-text-muted tabular">
                            {fmtInt(who.rows)} <span className="sr-only">memories</span>
                          </span>
                        </Button>
                      </Unavailable>
                    ) : (
                      <>
                        <NameBadge name={who.name} />
                        {fmtInt(who.rows)}
                      </>
                    )}
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
      Memories, runs and the users, agents and authors are counted for user{" "}
      <span className="font-mono break-all">{user_id}</span> only, as your token reads; documents and members are the
      whole namespace's.
    </p>
  );
}
