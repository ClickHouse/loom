import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { TextField } from "@clickhouse/click-ui/TextField";
import type { InspectSubject } from "../../../api";
import { Unavailable } from "../../../components/ui";
import { INSPECTED_AS, SUBJECT_COPY, shownName } from "../subjects";
import { useChat } from "../chatStore";
import { chatOff, useDock } from "../../../lib/assistantDock";
import { ROLE_LOADING } from "../../../lib/callReason";
import { useStore } from "../../../lib/state";
import { lastInventory, startStarter, useCardGate } from "./cardStore";
import { askState, setAlsoAsk, useStarterPrefs } from "./prefs";
import { DEFAULT_WINDOW, FIRST_STARTERS, PHONE_STARTERS, startersFor, type Starter } from "./starters";
import type { CardArgs } from "./types";

const SUBJECT_OPTIONS = (Object.keys(SUBJECT_COPY) as InspectSubject[]).map((value) => ({
  value,
  label: SUBJECT_COPY[value].label,
}));

/**
 * The starters: one full-width row per question Loom's own reads answer, the page's own
 * first (cards/starters.ts startersFor). Choosing one reads its card with the person's token
 * and, while "Also ask the model" is on and the assistant can answer, asks the model its
 * question too (which spends a model answer); with the chat off, or while the assistant
 * cannot answer, there is no such box, and a starter reads only Loom. A starter that takes
 * an id or an entity opens a small form first. Four show (three on the phone sheet), and
 * "More questions", a disclosure, shows the rest in place.
 */
export function Starters({
  namespace,
  phone,
  cannotAnswer,
  noField = false,
}: {
  namespace: string;
  phone: boolean;
  /** Why the assistant cannot answer now; null when it can. */
  cannotAnswer: string | null;
  /** The panel shows no question field (chat not set up or failing): an open's focus is the
   *  first starter's, as with the chat off. */
  noField?: boolean;
}) {
  const alsoAsk = useStarterPrefs((s) => s.alsoAsk);
  const answering = useChat((s) => s.answering);
  const gate = useCardGate(namespace);
  const view = useStore((s) => s.view);
  const [more, setMore] = useState(false);
  const [form, setForm] = useState<Starter["id"] | null>(null);
  const list = useRef<HTMLUListElement>(null);
  const costId = useId();
  const listId = useId();

  // With no question field (the chat off, not set up or failing) an open's focus is the
  // first starter's, taken once it renders.
  const focusField = useDock((s) => s.focusField);
  const fieldless = noField || chatOff();
  useEffect(() => {
    if (!focusField || !fieldless) return;
    list.current?.querySelector<HTMLElement>("[data-starter]")?.focus();
    useDock.setState({ focusField: false });
  }, [focusField, fieldless]);

  // An old link to the Memories Insights tab: the audit's card, read once the role is known,
  // with nothing asked (lib/assistantDock.ts).
  const startersAudit = useDock((s) => s.startersAudit);
  const auditGate = gate("namespace.audit");
  useEffect(() => {
    if (!startersAudit || auditGate === ROLE_LOADING) return;
    useDock.setState({ startersAudit: false });
    startStarter("namespace.audit", { windowSeconds: DEFAULT_WINDOW }, { alsoAsk: false, gate: auditGate, namespace });
  }, [startersAudit, auditGate, namespace]);

  const { asks, unanswered } = askState(alsoAsk, cannotAnswer);
  const all = startersFor(view);
  const first = phone ? PHONE_STARTERS : FIRST_STARTERS;
  // A form open on a starter "More questions" would hide keeps it shown.
  const shown = more || all.slice(first).some((s) => s.id === form) ? all : all.slice(0, first);
  const choose = (s: Starter, args: CardArgs = {}) => {
    const windowed = s.op === "namespace.audit" || s.op === "mentor.brief";
    startStarter(s.op, windowed ? { windowSeconds: DEFAULT_WINDOW, ...args } : args, {
      alsoAsk: asks,
      gate: gate(s.op),
      namespace,
      unanswered,
    });
  };
  // A starter is off only when it can do nothing: its read is not allowed and nothing is asked.
  const off = (s: Starter) => (answering ? "An answer is still coming in." : asks ? null : gate(s.op));

  return (
    <div className="space-y-2" data-testid="assistant-starters">
      <ul ref={list} id={listId} className="space-y-1.5" aria-label="Starters">
        {shown.map((s) => (
          <li key={s.id} className="space-y-2">
            <Unavailable reason={off(s)}>
              <Button
                type="secondary"
                htmlType="button"
                data-starter={s.id}
                fillWidth
                align="left"
                iconLeft="sparkle"
                className="text-left whitespace-normal h-auto"
                aria-expanded={s.id === "subject" || s.id === "graph" ? form === s.id : undefined}
                onClick={() =>
                  s.id === "subject" || s.id === "graph" ? setForm(form === s.id ? null : s.id) : choose(s)
                }
              >
                {s.label}
              </Button>
            </Unavailable>
            {form === "subject" && s.id === "subject" && <SubjectForm off={off(s)} onAsk={(args) => choose(s, args)} />}
            {form === "graph" && s.id === "graph" && <EntityForm onAsk={(args) => choose(s, args)} />}
          </li>
        ))}
      </ul>
      {all.length > first && (
        <Button
          type="empty"
          htmlType="button"
          iconRight={more ? "chevron-up" : "chevron-down"}
          aria-expanded={more}
          aria-controls={listId}
          onClick={() => setMore((v) => !v)}
        >
          More questions
        </Button>
      )}
      {/* With the chat off nothing asks a model, and nothing costs one; while the assistant
          cannot answer the box would do nothing, so it is not shown. */}
      {!chatOff() && cannotAnswer === null && (
        <div className="space-y-0.5">
          <Checkbox
            label="Also ask the model"
            checked={asks}
            onCheckedChange={(c) => setAlsoAsk(c === true)}
            aria-describedby={costId}
          />
          <p id={costId} className="pl-6 text-xs text-text-muted">
            {asks ? "Uses this namespace's model budget." : "A starter reads only Loom: no model is called."}
          </p>
        </div>
      )}
    </div>
  );
}

/** Whose memories: a subject and its id, the id suggested from the last inventory card. */
function SubjectForm({
  off,
  onAsk,
}: {
  /** Why nothing can be done now (the read not allowed and nothing asked). */
  off: string | null;
  onAsk: (args: { subject: InspectSubject; id: string }) => void;
}) {
  const [subject, setSubject] = useState<InspectSubject>("user");
  const [id, setId] = useState("");
  // One subject form shows at a time.
  const listId = "assistant-subject-suggestions";
  const turns = useChat((s) => s.turns);
  const copy = SUBJECT_COPY[subject];
  const suggestions = lastInventory(turns)?.populations.find((p) => INSPECTED_AS[p.population] === subject)?.top ?? [];
  const suggest = suggestions.length > 0;
  const blocked = off ?? (id.trim() ? null : suggest && copy.pick ? copy.pick : copy.empty);
  return (
    <form
      className="flex flex-wrap items-end gap-3 pl-1"
      aria-label="Whose memories"
      onSubmit={(e) => {
        e.preventDefault();
        if (!blocked) onAsk({ subject, id: id.trim() });
      }}
    >
      <ButtonGroup
        aria-label="Whose memories"
        options={SUBJECT_OPTIONS}
        selected={subject}
        onClick={(v) => {
          if (v === subject) return;
          setSubject(v as InspectSubject);
          setId("");
        }}
      />
      <div className="w-64 max-w-full">
        <TextField
          id="assistant-subject-id"
          label={copy.field}
          placeholder={suggest ? "Pick or type" : undefined}
          list={suggest ? listId : undefined}
          autoComplete="off"
          value={id}
          onChange={(v) => setId(v)}
        />
        {suggest && (
          <datalist id={listId}>
            {suggestions.map((s) => (
              <option key={s.name} value={s.name}>
                {shownName(subject, s.name)}
              </option>
            ))}
          </datalist>
        )}
      </div>
      <Unavailable reason={blocked}>
        <Button type="primary" htmlType="submit">
          Inspect memories
        </Button>
      </Unavailable>
    </form>
  );
}

/** Where the graph starts: an entity, or none for the graph's own starting points. */
function EntityForm({ onAsk }: { onAsk: (args: { entity: string }) => void }) {
  const [entity, setEntity] = useState("");
  return (
    <form
      className="flex flex-wrap items-end gap-3 pl-1"
      aria-label="Relationships"
      onSubmit={(e) => {
        e.preventDefault();
        onAsk({ entity: entity.trim() });
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
      <Button type="primary" htmlType="submit">
        Show relationships
      </Button>
    </form>
  );
}
