import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  inspectMemories,
  namespaceInventory,
  tuneNamespace,
  type InspectSubject,
  type MemoryInspection,
  type NamespaceInventory,
  type NamespaceTune,
  storedToken,
} from "../api";
import { ScopeBadge } from "../components/ScopeBadge";
import { BusyButton } from "../components/ui/BusyButton";
import { ListTime } from "../components/ui/ListTime";
import { Loading, PageHeader, Panel, SectionHeading, Unavailable } from "../components/ui";
import { PageContainer } from "../layout/PageContainer";
import { claimsFromToken } from "../lib/boot";
import { canCall } from "../lib/canCall";
import { openGraphAt } from "../lib/graphLink";
import { useNamespaceRole } from "../lib/namespaceRole";
import { useStore } from "../lib/state";
import { Inspection } from "./assistant/Inspection";
import { InventoryPopulations, InventoryScopeNote, InventorySections } from "./assistant/Inventory";
import { ReviewChanges } from "./assistant/ReviewChanges";
import { useScopedRead } from "./assistant/useScopedRead";

/**
 * Assistant — inspect what a user, agent or session remembers, and draft changes to the
 * categories, instructions and guide that create those memories. Every read is
 * deterministic (no model) and read-only: namespace.inventory, memory.inspect and
 * namespace.tune, each on a click, never on mount. A draft is never applied here.
 */

const SUBJECTS: Array<{ value: InspectSubject; label: string }> = [
  { value: "user", label: "User" },
  { value: "agent", label: "Agent" },
  { value: "session", label: "Session" },
];

type Starter = "relationships" | "inventory" | "analyze" | "users" | "configure";

const STARTERS: Array<{ id: Starter; label: string }> = [
  { id: "relationships", label: "Show relationships between facts" },
  { id: "inventory", label: "What is in this project?" },
  { id: "analyze", label: "Analyze one user's memories" },
  { id: "users", label: "Who are the users in this project?" },
  { id: "configure", label: "Configure this project" },
];

export function Assistant() {
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity?.namespace ?? "");
  const scope = `${identity?.org}/${identity?.namespace}`;
  const inventory = useScopedRead<NamespaceInventory>(scope);
  const inspection = useScopedRead<MemoryInspection>(scope);
  const tune = useScopedRead<NamespaceTune>(scope);
  // The open starter, with the namespace it was opened on: another namespace shows none.
  const [opened, setOpened] = useState<{ scope: string; id: Starter } | null>(null);
  const starter = opened?.scope === scope ? opened.id : null;
  const [subject, setSubject] = useState<InspectSubject>("user");
  const [subjectId, setSubjectId] = useState("");
  const [entity, setEntity] = useState("");

  if (!identity) return null;
  const where = { org: identity.org, namespace: identity.namespace };
  // Each read is gated on its own op. All three are VIEWER, which every namespace role
  // reaches, so a canCall refusal means the role is unknown (list_mine not read, failed, or
  // with no row for the namespace). The server refuses memory.inspect and namespace.tune to a
  // token pinned to one user or session (neither is a scoped op). The token's claims say so
  // up front; for an end-user token, or behind a proxy (no token here), only the inventory's
  // answer does.
  const claims = claimsFromToken(storedToken());
  const narrowed =
    claims?.readScope != null || (inventory.data !== null && inventory.data.scope.read_scope !== "namespace");
  // A scoped read under `own_session`, or under `own_user` with no `read_user_id`, needs the
  // session or user id in the request, which this page does not send: the server refuses it.
  const incomplete = claims?.readScope === "own_session" || (claims?.readScope === "own_user" && !claims.readUserId);
  const gate = (op: "namespace.inventory" | "memory.inspect" | "namespace.tune") =>
    !canCall(op, role, whoami)
      ? "Your role on this namespace is not known."
      : incomplete && op === "namespace.inventory"
        ? "Your token's read scope needs a user or session id this page does not send."
        : narrowed && op !== "namespace.inventory"
          ? "Your token is bound to one user or session; inspecting and drafting need a token for the whole namespace."
          : null;
  const starterGate: Record<Starter, string | null> = {
    relationships: null,
    inventory: gate("namespace.inventory"),
    users: gate("namespace.inventory"),
    analyze: gate("memory.inspect"),
    configure: gate("namespace.tune"),
  };
  const inspectBlocked = gate("memory.inspect") ?? (subjectId.trim() ? null : "Enter an id to inspect.");

  const start = (id: Starter) => {
    setOpened({ scope, id });
    // Each starter reads again on a click, so its answer is as fresh as the click.
    if (id === "inventory" || id === "users") inventory.run((signal) => namespaceInventory(where, { signal }));
    if (id === "analyze") {
      setSubject("user");
      document.getElementById("assistant-subject-id")?.focus();
    }
    if (id === "configure") tune.run((signal) => tuneNamespace(where, { signal }));
  };
  const runInspect = () => {
    const id = subjectId.trim();
    if (id) inspection.run((signal) => inspectMemories(where, subject, id, { signal }));
  };

  return (
    <PageContainer>
      <PageHeader title="Assistant">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>
      <p className="text-sm text-text-muted mb-4 max-w-[72ch]">
        Inspect what a user, agent or session remembers in this namespace, then draft changes to the categories,
        extraction instructions and guide that create those memories. Everything here reads; nothing is changed, and a
        draft is applied only by a person who runs its op.
      </p>

      <Panel as="section" aria-labelledby="assistant-scope" className="mb-5">
        <SectionHeading
          id="assistant-scope"
          level={2}
          label="Scope"
          hint="A user, agent or session filters rows inside this namespace; it never reaches rows your token cannot read. Switch namespaces in the header."
        />
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!inspectBlocked) runInspect();
          }}
        >
          <ButtonGroup
            aria-label="Subject"
            options={SUBJECTS}
            selected={subject}
            onClick={(v) => setSubject(v as InspectSubject)}
          />
          <div className="w-64">
            <TextField
              id="assistant-subject-id"
              label={`${SUBJECTS.find((s) => s.value === subject)?.label} id`}
              value={subjectId}
              onChange={(v) => setSubjectId(v)}
            />
          </div>
          {inspectBlocked ? (
            <Unavailable reason={inspectBlocked}>
              <Button type="primary" htmlType="submit">
                Inspect
              </Button>
            </Unavailable>
          ) : (
            // BusyButton keeps the focus while the read runs; Unavailable needs a ref it
            // does not forward, so a blocked Inspect is the plain button.
            <BusyButton type="primary" htmlType="submit" busy={inspection.busy}>
              Inspect
            </BusyButton>
          )}
        </form>
      </Panel>

      <Panel as="section" aria-labelledby="assistant-starters" className="mb-5">
        <SectionHeading id="assistant-starters" level={2} label="Start with" />
        <div className="flex flex-wrap gap-2">
          {STARTERS.map((s) => (
            <Unavailable key={s.id} reason={starterGate[s.id]}>
              <Button
                type="secondary"
                htmlType="button"
                onClick={() => start(s.id)}
                data-focus-key={`assistant-starter-${s.id}`}
              >
                {s.label}
              </Button>
            </Unavailable>
          ))}
        </div>
      </Panel>

      {starter === "relationships" && (
        <Panel as="section" aria-labelledby="assistant-graph" className="mb-5">
          <SectionHeading
            id="assistant-graph"
            level={2}
            label="Relationships"
            hint="The graph walks the facts you can read from one entity, with each edge's source facts and validity. A connection is not a cause."
          />
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              // Back lands on the page with its panels closed, focused on the starter.
              if (entity.trim())
                openGraphAt(entity, { from: "Assistant", focusKey: "assistant-starter-relationships" });
            }}
          >
            <div className="w-64">
              <TextField id="assistant-entity" label="Entity" value={entity} onChange={(v) => setEntity(v)} />
            </div>
            <Unavailable reason={entity.trim() ? null : "Enter an entity to walk from."}>
              <Button type="secondary" htmlType="submit">
                Open the graph
              </Button>
            </Unavailable>
          </form>
        </Panel>
      )}

      {(starter === "inventory" || starter === "users") && (
        <Panel as="section" aria-labelledby="assistant-inventory-heading" className="mb-5">
          <SectionHeading
            id="assistant-inventory-heading"
            level={2}
            label={starter === "inventory" ? "What this namespace holds" : "Who appears in this namespace"}
          />
          <ReadState read={inventory} label="the inventory" />
          {inventory.data && (
            <div aria-busy={inventory.busy || undefined}>
              <InventoryScopeNote inventory={inventory.data} />
              {starter === "inventory" ? (
                <InventorySections inventory={inventory.data} />
              ) : (
                <InventoryPopulations inventory={inventory.data} />
              )}
              <ReadAt time={inventory.data.computed_at} />
            </div>
          )}
        </Panel>
      )}

      {(inspection.busy || inspection.data || inspection.error) && (
        <Panel as="section" aria-labelledby="assistant-inspection-heading" className="mb-5">
          <SectionHeading id="assistant-inspection-heading" level={2} label="Inspection" />
          <ReadState read={inspection} label="the inspection" />
          {inspection.data && (
            <div aria-busy={inspection.busy || undefined}>
              <Inspection result={inspection.data} />
              <ReadAt time={inspection.data.computed_at} />
            </div>
          )}
        </Panel>
      )}

      <Panel as="section" aria-labelledby="assistant-review-heading" className="mb-5">
        <SectionHeading
          id="assistant-review-heading"
          level={2}
          label="Review changes"
          count={tune.data?.proposals.length}
          hint="Drafts from namespace.tune, each naming its target, current value, evidence and the op that would apply it. Apply is not in this release."
        />
        <ReadState read={tune} label="the drafts" />
        {!(tune.busy && !tune.data) && (
          <div aria-busy={tune.busy || undefined}>
            <ReviewChanges tune={tune.data} />
            {tune.data && <ReadAt time={tune.data.computed_at} />}
          </div>
        )}
      </Panel>
    </PageContainer>
  );
}

/** The first read shows a loader; a read again keeps the last answer on screen, marked busy. */
function ReadState({ read, label }: { read: { busy: boolean; error: string | null; data: unknown }; label: string }) {
  if (read.error)
    return (
      <p role="alert" className="text-sm text-danger mb-2">
        Couldn't load {label}: {read.error}
      </p>
    );
  if (read.busy && !read.data) return <Loading layout="inline" label={`Loading ${label}`} />;
  return null;
}

function ReadAt({ time }: { time: string }) {
  return (
    <p className="mt-3 text-xs text-text-muted">
      Read <ListTime time={time} />
    </p>
  );
}
