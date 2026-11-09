import { useCallback, useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  inspectKnowledge,
  inspectMemories,
  namespaceInventory,
  tuneNamespace,
  type InspectSubject,
  type KnowledgeInspection,
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
import { canCall } from "../lib/canCall";
import { openGraphAt } from "../lib/graphLink";
import { useNamespaceRoleRead } from "../lib/namespaceRole";
import { useStore } from "../lib/state";
import { AssistantChat } from "./assistant/Chat";
import { DocumentInspection } from "./assistant/DocumentInspection";
import { Inspection } from "./assistant/Inspection";
import { InventoryPopulations, InventoryScopeNote, InventorySections } from "./assistant/Inventory";
import { ReviewChanges } from "./assistant/ReviewChanges";
import { INSPECTED_AS, SUBJECT_COPY, shownName } from "./assistant/subjects";
import { tokenReadScope } from "./assistant/tokenScope";
import { useScopedRead } from "./assistant/useScopedRead";

/**
 * Assistant — what a namespace holds and who appears in it (namespace.inventory, read on
 * load), what one user, agent or session remembers (memory.inspect), what its documents are
 * (knowledge.inspect), drafts of changes to what creates them (namespace.tune), and a chat
 * with the assistant (`/v1/assistant/chat/completions`, `assistant/Chat.tsx`). Every read but
 * the chat is deterministic (no model); all are read-only, and a draft is never applied here.
 */

const SUBJECT_OPTIONS = (Object.keys(SUBJECT_COPY) as InspectSubject[]).map((value) => ({
  value,
  label: SUBJECT_COPY[value].label,
}));

export function Assistant() {
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const { role, loading: roleLoading } = useNamespaceRoleRead(identity?.namespace ?? "");
  // The answers belong to the namespace and to the token's read scope: a token signed in
  // again with another scope (the page stays mounted) drops what the old one read.
  const tokenScope = tokenReadScope(storedToken());
  const scope = `${identity?.org}/${identity?.namespace}/${tokenScope?.readScope ?? "-"}/${tokenScope?.readUserId ?? "-"}`;
  // The chat's conversation is the signed-in person's too: it remounts and is kept under one key.
  const chatKey = `${whoami?.subject ?? "-"}/${scope}`;
  const inventory = useScopedRead<NamespaceInventory>(scope);
  const inspection = useScopedRead<MemoryInspection>(scope);
  const documents = useScopedRead<KnowledgeInspection>(scope);
  const tune = useScopedRead<NamespaceTune>(scope);
  const [subject, setSubject] = useState<InspectSubject>("user");
  const [subjectId, setSubjectId] = useState("");
  const [entity, setEntity] = useState("");

  // Each read is gated on its own op. All four are VIEWER, so a canCall refusal means the role
  // is unknown; the server refuses memory.inspect and namespace.tune to a pinned token. With
  // no claim to read (an end-user token, or behind a proxy), the inventory's answer says so.
  const narrowed =
    tokenScope?.readScope != null || (inventory.data !== null && inventory.data.scope.read_scope !== "namespace");
  // A scoped read under `own_session`, or under `own_user` with no `read_user_id`, needs the
  // session or user id in the request, which this page does not send: the server refuses it.
  const incomplete =
    tokenScope?.readScope === "own_session" || (tokenScope?.readScope === "own_user" && !tokenScope.readUserId);
  const gate = (
    op: "namespace.inventory" | "knowledge.inspect" | "memory.inspect" | "namespace.tune" | "assistant.ask",
  ) => {
    const scoped = op === "namespace.inventory" || op === "knowledge.inspect";
    if (roleLoading) return "Your role on this namespace is still loading.";
    if (!canCall(op, role, whoami)) return "Your role on this namespace is not known.";
    if (incomplete && scoped)
      return "Your token needs each request to name its user or session, which this page does not send.";
    if (narrowed && op === "assistant.ask")
      return "Your token is limited to one user or session; the assistant needs a token for the whole namespace.";
    if (narrowed && !scoped)
      return "Your token is limited to one user or session; inspecting memories and drafting need a token for the whole namespace.";
    return null;
  };
  const overviewGate = gate("namespace.inventory");
  const memoryGate = gate("memory.inspect");
  const documentsGate = gate("knowledge.inspect");
  const tuneGate = gate("namespace.tune");
  const chatGate = gate("assistant.ask");
  const copy = SUBJECT_COPY[subject];
  const suggestions = inventory.data?.populations.find((p) => INSPECTED_AS[p.population] === subject)?.top ?? [];
  const suggest = suggestions.length > 0;
  const emptyReason = suggest && copy.pick ? copy.pick : copy.empty;
  // Only users and agents are picked; authors and members are listed, not inspected.
  const pickable = inventory.data?.populations.some((p) => INSPECTED_AS[p.population] && p.top.length > 0) ?? false;
  const inspectBlocked = memoryGate ?? (subjectId.trim() ? null : emptyReason);

  const org = identity?.org ?? "";
  const namespace = identity?.namespace ?? "";
  const { run: runInventory } = inventory;
  const readOverview = useCallback(
    () => runInventory((signal) => namespaceInventory({ org, namespace }, { signal })),
    [runInventory, org, namespace],
  );
  // The overview is the page's first answer: read on load, as soon as the role allows it,
  // again when the namespace or token scope changes, and on Refresh.
  useEffect(() => {
    if (org && overviewGate === null) readOverview();
  }, [org, overviewGate, readOverview]);
  // An id or entity picked or typed in one namespace is not meant for the next one. A new
  // token on the same namespace drops the answers (they are keyed by scope) but keeps what
  // was typed: it still names someone in this namespace.
  const [formNamespace, setFormNamespace] = useState(`${org}/${namespace}`);
  if (formNamespace !== `${org}/${namespace}`) {
    setFormNamespace(`${org}/${namespace}`);
    setSubjectId("");
    setEntity("");
  }

  if (!identity) return null;
  const where = { org, namespace };

  const inspect = (who: InspectSubject, id: string) => {
    inspection.run((signal) => inspectMemories(where, who, id, { signal }));
  };
  // A pick in the overview fills the form, inspects, and moves the focus to the answer,
  // which is further down the page. The read runs inside flushSync: the Memories heading
  // mounts once it is busy, and must exist before it is focused.
  const pick = (who: InspectSubject, id: string) => {
    flushSync(() => {
      setSubject(who);
      setSubjectId(id);
      inspect(who, id);
    });
    document.getElementById("assistant-inspection-heading")?.focus();
  };

  return (
    <PageContainer>
      <PageHeader title="Assistant">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>
      <p className="text-sm text-text-muted mb-4 max-w-[72ch]">
        Ask the assistant about this namespace, see what it holds and what each user, agent or session remembers, then
        draft improvements. Nothing on this page changes your data.
      </p>

      <Panel as="section" aria-labelledby="assistant-overview" className="mb-5">
        <SectionHeading
          id="assistant-overview"
          level={2}
          label="Overview"
          actions={
            <Unavailable reason={overviewGate}>
              <BusyButton type="secondary" htmlType="button" busy={inventory.busy} onClick={readOverview}>
                Refresh
              </BusyButton>
            </Unavailable>
          }
        />
        {roleLoading ? (
          <Loading layout="inline" label="Loading the overview" />
        ) : overviewGate ? (
          <p className="text-sm text-text-muted">{overviewGate}</p>
        ) : (
          <>
            <ReadState read={inventory} label="the overview" />
            {inventory.data && (
              <div aria-busy={inventory.busy || undefined} className="space-y-4">
                <InventoryScopeNote inventory={inventory.data} />
                <section aria-labelledby="assistant-holds">
                  <SectionHeading id="assistant-holds" level={3} label="What this namespace holds" />
                  <InventorySections inventory={inventory.data} />
                </section>
                <section aria-labelledby="assistant-people">
                  <SectionHeading
                    id="assistant-people"
                    level={3}
                    label="Who appears here"
                    hint={pickable && !memoryGate ? "Pick a user or agent to see what it remembers." : undefined}
                  />
                  <InventoryPopulations inventory={inventory.data} onPick={pick} pickBlocked={memoryGate} />
                </section>
                <ReadAt time={inventory.data.computed_at} />
              </div>
            )}
          </>
        )}
      </Panel>

      <Panel as="section" aria-labelledby="assistant-chat" className="mb-5">
        <SectionHeading id="assistant-chat" level={2} label="Ask the assistant" />
        <p className="text-sm text-text-muted mb-3 max-w-[72ch]">
          Ask about what this namespace holds. The assistant reads with your access and calls a model, which counts
          against this namespace's model budget. The conversation stays in this tab until it reloads: Loom stores none
          of it.
        </p>
        <AssistantChat key={chatKey} conversationKey={chatKey} namespace={namespace} blocked={chatGate} />
      </Panel>

      <Panel as="section" aria-labelledby="assistant-inspect" className="mb-5">
        <SectionHeading id="assistant-inspect" level={2} label="Inspect" />
        <form
          className="flex flex-wrap items-end gap-3"
          aria-label="Inspect memories"
          onSubmit={(e) => {
            e.preventDefault();
            if (!inspectBlocked) inspect(subject, subjectId.trim());
          }}
        >
          <ButtonGroup
            aria-label="Whose memories"
            options={SUBJECT_OPTIONS}
            selected={subject}
            onClick={(v) => {
              if (v === subject) return; // ButtonGroup calls onClick for the selected option too
              setSubject(v as InspectSubject);
              setSubjectId(""); // a user id is not an agent name or a session id
            }}
          />
          <div className="w-64 max-w-full">
            {/* A text input with a datalist is the native combobox: it suggests the users or
                agents the overview found and still takes any other id. */}
            <TextField
              id="assistant-subject-id"
              label={copy.field}
              placeholder={suggest ? "Pick or type" : undefined}
              list={suggest ? "assistant-subject-suggestions" : undefined}
              autoComplete="off"
              value={subjectId}
              onChange={(v) => setSubjectId(v)}
            />
            {suggest && (
              <datalist id="assistant-subject-suggestions">
                {suggestions.map((s) => (
                  <option key={s.name} value={s.name}>
                    {shownName(subject, s.name)}
                  </option>
                ))}
              </datalist>
            )}
          </div>
          <Unavailable reason={inspectBlocked}>
            <BusyButton type="primary" htmlType="submit" busy={inspection.busy}>
              Inspect memories
            </BusyButton>
          </Unavailable>
        </form>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Unavailable reason={documentsGate}>
            <BusyButton
              type="secondary"
              htmlType="button"
              busy={documents.busy}
              onClick={() => documents.run((signal) => inspectKnowledge(where, { signal }))}
              // While blocked, Unavailable's reason replaces this description.
              aria-describedby="assistant-documents-what"
            >
              Inspect documents
            </BusyButton>
          </Unavailable>
          <p id="assistant-documents-what" className="text-sm text-text-muted">
            Every document here: formats, what search serves, and ingest health.
          </p>
        </div>
      </Panel>

      {(inspection.busy || inspection.data || inspection.error) && (
        <Panel as="section" aria-labelledby="assistant-inspection-heading" className="mb-5">
          <SectionHeading id="assistant-inspection-heading" level={2} label="Memories" focusable />
          <ReadState read={inspection} label="the memories" />
          {inspection.data && (
            <div aria-busy={inspection.busy || undefined}>
              <Inspection result={inspection.data} />
              <ReadAt time={inspection.data.computed_at} />
            </div>
          )}
        </Panel>
      )}

      {(documents.busy || documents.data || documents.error) && (
        <Panel as="section" aria-labelledby="assistant-documents-heading" className="mb-5">
          <SectionHeading id="assistant-documents-heading" level={2} label="Documents" />
          <ReadState read={documents} label="the documents" />
          {documents.data && (
            <div aria-busy={documents.busy || undefined}>
              <DocumentInspection result={documents.data} />
              <ReadAt time={documents.data.computed_at} />
            </div>
          )}
        </Panel>
      )}

      <Panel as="section" aria-labelledby="assistant-graph" className="mb-5">
        <SectionHeading
          id="assistant-graph"
          level={2}
          label="Relationships"
          hint="The graph shows how the facts in your memories connect."
        />
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            // Back lands on this page, focused on the button. With no entity the graph
            // opens on its own starting points and suggestions.
            openGraphAt(entity, { from: "Assistant", focusKey: "assistant-open-graph" });
          }}
        >
          <div className="w-64 max-w-full">
            <TextField
              id="assistant-entity"
              label="Start from (optional)"
              placeholder="A person, project or thing"
              value={entity}
              onChange={(v) => setEntity(v)}
            />
          </div>
          <Button type="secondary" htmlType="submit" data-focus-key="assistant-open-graph">
            Open the graph
          </Button>
        </form>
      </Panel>

      <Panel as="section" aria-labelledby="assistant-improve" className="mb-5">
        <SectionHeading id="assistant-improve" level={2} label="Improve this namespace" />
        <p className="text-sm text-text-muted mb-3 max-w-[72ch]">
          Draft changes to the categories, guide and extraction instructions that shape new memories, and to how
          documents are split. Drafts are suggestions: each says what to change, why, and who applies it.
        </p>
        <Unavailable reason={tuneGate}>
          <BusyButton
            type="secondary"
            htmlType="button"
            busy={tune.busy}
            onClick={() => tune.run((signal) => tuneNamespace(where, { signal }))}
          >
            Draft improvements
          </BusyButton>
        </Unavailable>
        <div className="mt-3">
          <ReadState read={tune} label="the drafts" />
        </div>
        {tune.data && (
          <section className="mt-4" aria-labelledby="assistant-review-heading" aria-busy={tune.busy || undefined}>
            <SectionHeading
              id="assistant-review-heading"
              level={3}
              label="Review changes"
              count={tune.data.proposals.length}
            />
            <ReviewChanges tune={tune.data} />
            <ReadAt time={tune.data.computed_at} />
          </section>
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
