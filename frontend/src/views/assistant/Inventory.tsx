import { type ReactNode, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import type {
  InspectSubject,
  InventoryPlane,
  InventoryPopulationName,
  InventorySection,
  NamespaceInventory,
} from "../../api";
import { NameBadge } from "../../components/AgentBadge";
import { Stat, Unavailable } from "../../components/ui";
import { ListTime } from "../../components/ui/ListTime";
import { useClamped } from "../../components/ui/TextClamp";
import { fmtInt, sentenceCase } from "../../lib/format";
import { INSPECTED_AS, shownName } from "./subjects";

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
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
        {inventory.sections.map((s) => (
          <div key={s.plane} data-plane={s.plane}>
            <Stat
              label={PLANE_LABEL[s.plane] ?? s.plane}
              value={s.count === null ? "—" : fmtInt(s.count)}
              hint={planeHint(s)}
            />
          </div>
        ))}
      </div>
      <p className="mt-2 text-xs text-text-muted">
        {inventory.capabilities
          .map((c) => `${c.name === "llm" ? "Model" : "Embedder"}: ${c.enabled ? "enabled" : "not enabled"}`)
          .join(" · ")}
      </p>
    </div>
  );
}

/** Under a count: why it was not counted (the server's reason says so), else when the
 *  newest row was written. */
function planeHint(s: InventorySection): ReactNode {
  if (!s.complete)
    return (
      <span className="break-words" data-testid="inventory-incomplete">
        {sentenceCase(s.reason) || "Not counted"}
      </span>
    );
  if (!s.latest_at) return null;
  return (
    <>
      Newest <ListTime time={s.latest_at} />
    </>
  );
}

const POPULATION_LABEL: Record<InventoryPopulationName, { title: string; what: string }> = {
  subjects: { title: "Users", what: "users that memories are filed under" },
  agents: { title: "Agents", what: "agents that memories were written under" },
  authors: { title: "Authors", what: "people and services that wrote memories" },
  members: { title: "Members", what: "people with access to this namespace" },
};

const ORDER: InventoryPopulationName[] = ["subjects", "agents", "authors", "members"];
// A population this page does not know yet goes last.
const rank = (p: InventoryPopulationName) => {
  const i = ORDER.indexOf(p);
  return i < 0 ? ORDER.length : i;
};

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
  const populations = [...inventory.populations].sort((a, b) => rank(a.population) - rank(b.population));
  return (
    <ul className="space-y-3" data-testid="assistant-populations">
      {populations.map((p) => {
        const label = POPULATION_LABEL[p.population] ?? { title: p.population, what: "" };
        const subject = INSPECTED_AS[p.population];
        return (
          <li key={p.population} data-population={p.population}>
            <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
              <span className="font-medium text-text">{label.title}</span>
              <span className="tabular text-text">{p.total === null ? "—" : fmtInt(p.total)}</span>
              <span className="text-xs text-text-muted">{label.what}</span>
            </div>
            {p.total !== null && p.top.length > 0 && p.total > p.top.length && (
              <p className="text-xs text-text-muted">The {fmtInt(p.top.length)} with the most memories are shown.</p>
            )}
            {!p.complete && <p className="text-xs text-text-muted">{sentenceCase(p.reason) || "Not complete"}</p>}
            {p.top.length > 0 && (
              <ul className="mt-1.5 flex flex-wrap gap-1.5" aria-label={`${label.title}, most memories first`}>
                {p.top.map((who) => (
                  <li key={who.name}>
                    {subject ? (
                      <PickButton
                        subject={subject}
                        name={who.name}
                        rows={who.rows}
                        blocked={pickBlocked}
                        onPick={() => onPick(subject, who.name)}
                      />
                    ) : (
                      <span className="inline-flex items-center gap-1 text-xs text-text-muted tabular">
                        <NameBadge name={who.name} />
                        {fmtInt(who.rows)}
                      </span>
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

/** One user or agent to inspect. Its name is cut at the badges' width, with the whole id in
 *  the tip when cut: alone on an open pick, under the reason on a blocked one. */
function PickButton({
  subject,
  name,
  rows,
  blocked,
  onPick,
}: {
  subject: InspectSubject;
  name: string;
  rows: number;
  blocked: string | null;
  onPick: () => void;
}) {
  const [nameEl, setNameEl] = useState<HTMLSpanElement | null>(null);
  const shown = shownName(subject, name);
  const cut = useClamped(nameEl, shown);
  return (
    <Unavailable reason={blocked} tip={cut ? name : undefined}>
      {/* Click UI lays the label out as a flex row, so its text reads "ada12"; aria-label gives the spoken name. */}
      <Button
        type="secondary"
        htmlType="button"
        onClick={onPick}
        data-testid={`pick-${subject}`}
        aria-label={`${shown} ${fmtInt(rows)} ${rows === 1 ? "memory" : "memories"}`}
      >
        <span ref={setNameEl} className="agent-badge truncate">
          {shown}
        </span>
        <span className="ml-1.5 text-text-muted tabular">{fmtInt(rows)}</span>
      </Button>
    </Unavailable>
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
