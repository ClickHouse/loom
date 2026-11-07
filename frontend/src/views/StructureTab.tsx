import { useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Select } from "@clickhouse/click-ui/Select";
import { Table } from "@clickhouse/click-ui/Table";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  RECORD_LIMIT,
  clearProfileSchema,
  decideRecordSchema,
  getProfileSchema,
  listCategories,
  listOntology,
  listRecordSchemas,
  queryRecords,
  recordConflicts,
  recordHistory,
  registerCategory,
  registerOntologyTerm,
  registerRecordSchema,
  setProfileSchema,
  type Category,
  type OntologyEntry,
  type OntologyKind,
  type ProfileSchema,
  type RecordConflict,
  type RecordSchema,
  type SchemaField,
  type SchemaFieldType,
  type TypedRecord,
} from "../api/structure";
import { useIdentity, useStore } from "../lib/state";
import { useAbortableEffect } from "../lib/hooks";
import { humanizeError } from "../lib/format";
import { canCall } from "../lib/canCall";
import { useNamespaceRole } from "../lib/namespaceRole";
import { confirmAction } from "../components/ConfirmDialog";
import { toast } from "../components/Toast";
import { ListState } from "../components/ui/ListState";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, ListTime, Loading, Panel, SectionHeading } from "../components/ui";

/**
 * Settings → Structure: what a namespace declares about the shape of its memory. The
 * typed-record schemas (and the records written against them), the schema profiles are
 * built to, the graph's entity types and relations, and the category registry. Anyone
 * who reads the namespace sees them; each change is offered to the role its op asks.
 * Setup keys it by namespace, so a switch starts it afresh.
 */
export function StructureTab() {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const can = (op: Parameters<typeof canCall>[0]) => canCall(op, role, whoami);
  return (
    <div className="max-w-4xl space-y-6">
      <RecordSchemas canRegister={can("memory_schema.register")} canDecide={can("memory_schema.approve")} />
      <ProfileSchemaPanel canSet={can("profile_schema.set")} canClear={can("profile_schema.clear")} />
      <Ontology canRegister={can("namespace.register_ontology")} />
      <Categories canAdd={can("namespace.register_category")} />
    </div>
  );
}

/** One section's read: the rows, a failure with Retry, and a reload after a change. */
function useSection<T>(read: (signal: AbortSignal) => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  useAbortableEffect(
    (signal, live) => {
      setError(null);
      read(signal)
        .then((d) => live() && setData(d))
        .catch((e) => live() && setError(e));
    },
    [reload],
  );
  return { data, error, refresh: () => setReload((n) => n + 1) };
}

/** What a section shows before its rows: the failure with Retry, or that it is loading. */
function SectionState({
  error,
  loading,
  things,
  onRetry,
}: {
  error: unknown;
  loading: boolean;
  things: string;
  onRetry: () => void;
}) {
  if (error) return <ListState kind="error" things={things} error={error} onRetry={onRetry} />;
  return loading ? <Loading label={`Loading ${things}`} /> : null;
}

// ─── Record schemas ────────────────────────────────────────────────────

const SCHEMA_BADGE: Record<string, "warning" | "neutral"> = { proposed: "warning", rejected: "neutral" };

function RecordSchemas({ canRegister, canDecide }: { canRegister: boolean; canDecide: boolean }) {
  const identity = useIdentity();
  const { data, error, refresh } = useSection((signal) => listRecordSchemas(identity, { signal }));
  const [adding, setAdding] = useState(false);
  const [records, setRecords] = useState<string | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);

  const decide = async (name: string, decision: "approve" | "reject") => {
    setDeciding(name);
    try {
      await decideRecordSchema(identity, name, decision);
      toast(decision === "approve" ? `Approved ${name}` : `Rejected ${name}`, "ok");
      refresh();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setDeciding(null);
    }
  };

  return (
    <Panel>
      <SectionHeading
        label="Record schemas"
        level={2}
        caption="Named fields typed records are written against."
        actions={
          canRegister && (
            <Button type="secondary" htmlType="button" iconLeft="plus" onClick={() => setAdding(true)}>
              New schema
            </Button>
          )
        }
      />
      <SectionState error={error} loading={data === null} things="record schemas" onRetry={refresh} />
      {data?.schemas.length === 0 && <p className="mt-2 text-sm text-text-muted">No record schema yet.</p>}
      <ul className="mt-2 divide-y divide-border">
        {data?.schemas.map((s) => (
          <li key={s.name} className="flex flex-wrap items-center gap-2 py-2">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm">{s.name}</span>
                {s.status !== "active" && (
                  <Badge text={s.status} state={SCHEMA_BADGE[s.status] ?? "neutral"} size="sm" />
                )}
              </div>
              <div className="text-xs text-text-muted">{s.fields.map((f) => `${f.name}: ${f.type}`).join(", ")}</div>
            </div>
            {canDecide && s.status === "proposed" && (
              <>
                <Button
                  type="secondary"
                  htmlType="button"
                  disabled={deciding !== null}
                  onClick={() => void decide(s.name, "reject")}
                >
                  Reject
                </Button>
                <Button
                  type="secondary"
                  htmlType="button"
                  disabled={deciding !== null}
                  onClick={() => void decide(s.name, "approve")}
                >
                  Approve
                </Button>
              </>
            )}
            {s.status === "active" && (
              <Button type="empty" htmlType="button" onClick={() => setRecords(s.name)}>
                Records
              </Button>
            )}
          </li>
        ))}
      </ul>
      {adding && (
        <NewSchemaDialog
          existing={data?.schemas ?? []}
          onClose={(added) => {
            setAdding(false);
            if (added) refresh();
          }}
        />
      )}
      {records && <RecordsDialog schema={records} onClose={() => setRecords(null)} />}
    </Panel>
  );
}

const FIELD_TYPES: SchemaFieldType[] = ["string", "number", "integer", "boolean"];
const blankField = (): SchemaField => ({ name: "", type: "string", required: false, identity: false });

function NewSchemaDialog({ existing, onClose }: { existing: RecordSchema[]; onClose: (added: boolean) => void }) {
  const identity = useIdentity();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [fields, setFields] = useState<SchemaField[]>([blankField()]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const named = fields.filter((f) => f.name.trim());
  const ready = name.trim() !== "" && named.length > 0 && !busy;
  // Schemas are keyed by name: registering one that exists replaces its fields.
  const replaces = existing.some((s) => s.name === name.trim());
  const edit = (i: number, change: Partial<SchemaField>) =>
    setFields((fs) => fs.map((f, j) => (j === i ? { ...f, ...change } : f)));

  const submit = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await registerRecordSchema(
        identity,
        name.trim(),
        description.trim(),
        named.map((f) => ({ ...f, name: f.name.trim() })),
      );
      toast(`${replaces ? "Replaced" : "Registered"} ${name.trim()}`, "ok");
      onClose(true);
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  return (
    <Modal open onOpenChange={(v) => !v && onClose(false)} title="New record schema" className="w-full max-w-[640px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-4">
          <TextField id="schema-name" label="Name" value={name} onChange={setName} />
          {replaces && <Alert state="warning" text={`${name.trim()} exists: registering replaces its fields.`} />}
          <TextField id="schema-description" label="Description" value={description} onChange={setDescription} />
          <fieldset className="space-y-2">
            <legend className="text-sm text-text mb-1">Fields</legend>
            {fields.map((f, i) => (
              <div key={i} className="flex flex-wrap items-end gap-2">
                <TextField
                  id={`schema-field-${i}`}
                  label={`Field ${i + 1}`}
                  value={f.name}
                  onChange={(v) => edit(i, { name: v })}
                />
                <div className="w-32">
                  <Select
                    value={f.type}
                    onSelect={(v) => edit(i, { type: v as SchemaFieldType })}
                    triggerProps={{ "aria-label": `Type of field ${i + 1}` }}
                  >
                    {FIELD_TYPES.map((t) => (
                      <Select.Item key={t} value={t}>
                        {t}
                      </Select.Item>
                    ))}
                  </Select>
                </div>
                <Checkbox
                  label="Required"
                  checked={f.required}
                  onCheckedChange={(c) => edit(i, { required: c === true })}
                />
                <Checkbox
                  label="Identity"
                  checked={f.identity}
                  onCheckedChange={(c) => edit(i, { identity: c === true })}
                />
                {fields.length > 1 && (
                  <Button
                    type="empty"
                    htmlType="button"
                    iconLeft="trash"
                    aria-label={`Remove field ${i + 1}`}
                    onClick={() => setFields((fs) => fs.filter((_, j) => j !== i))}
                  />
                )}
              </div>
            ))}
            <Button
              type="empty"
              htmlType="button"
              iconLeft="plus"
              onClick={() => setFields((fs) => [...fs, blankField()])}
            >
              Add a field
            </Button>
          </fieldset>
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={() => onClose(false)}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={!ready} loading={busy}>
            Register
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}

function formatCell(v: unknown): string {
  return v === null || v === undefined ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v);
}

/** The records of one schema as they stand, and the fields where a newer record disagreed
 *  with the one it superseded; a disagreement opens the superseded record's versions. */
function RecordsDialog({ schema, onClose }: { schema: string; onClose: () => void }) {
  const identity = useIdentity();
  const [rows, setRows] = useState<{ rows: TypedRecord[]; truncated: boolean } | null>(null);
  const [conflicts, setConflicts] = useState<{ rows: RecordConflict[]; truncated: boolean } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [prior, setPrior] = useState<{ key: string; rows: TypedRecord[] } | null>(null);
  // The prior record asked for last: an earlier answer that lands late is dropped.
  const askedFor = useRef("");
  useAbortableEffect(
    (signal, live) => {
      Promise.all([queryRecords(identity, schema, { signal }), recordConflicts(identity, schema, { signal })])
        .then(([r, c]) => {
          if (!live()) return;
          setRows(r);
          setConflicts(c);
        })
        .catch((e) => live() && setError(e));
    },
    [schema],
  );
  const openPrior = async (c: RecordConflict) => {
    askedFor.current = c.prior_record_id;
    try {
      const r = await recordHistory(identity, c.prior_record_id);
      if (askedFor.current === c.prior_record_id) setPrior({ key: c.memory_key, rows: r.rows });
    } catch (e) {
      toast(`Couldn't read the prior record: ${humanizeError(e)}`, "error");
    }
  };
  const columns = [...new Set((rows?.rows ?? []).flatMap((r) => Object.keys(r.record)))];
  return (
    <Modal open onOpenChange={(v) => !v && onClose()} title={`Records of ${schema}`} className="w-full max-w-[880px]">
      {!!error && <Alert role="alert" state="danger" text={humanizeError(error)} />}
      {!error && rows === null && <Loading label="Loading records" />}
      {rows?.rows.length === 0 && <p className="text-sm text-text-muted">No record yet.</p>}
      {!!rows?.rows.length && (
        <>
          <Table
            aria-label={`Records of ${schema}`}
            size="sm"
            headers={columns.map((c) => ({ label: c }))}
            rows={rows.rows.map((r) => ({
              id: r.record_id,
              items: columns.map((c) => ({ label: <span className="text-xs">{formatCell(r.record[c])}</span> })),
            }))}
          />
          {rows.truncated && <p className="mt-1 text-xs text-text-muted">The first {RECORD_LIMIT} records.</p>}
        </>
      )}
      {!!conflicts?.rows.length && (
        <section className="mt-4">
          <SectionHeading label="Disagreements" level={3} count={conflicts.rows.length} />
          <ul className="mt-1 space-y-1 text-xs">
            {conflicts.rows.map((c) => (
              <li key={`${c.incoming_record_id}-${c.field_name}`} className="flex flex-wrap items-center gap-2">
                <span className="font-mono">{c.memory_key}</span>
                <span>
                  {c.field_name}: {formatCell(c.prior_value)} → {formatCell(c.incoming_value)}
                </span>
                {c.detected_at && <ListTime time={c.detected_at} quiet />}
                <Button
                  type="empty"
                  htmlType="button"
                  aria-label={`Prior record of ${c.memory_key}`}
                  onClick={() => void openPrior(c)}
                >
                  Prior record
                </Button>
              </li>
            ))}
          </ul>
          {conflicts.truncated && <p className="mt-1 text-xs text-text-muted">The first {RECORD_LIMIT}.</p>}
        </section>
      )}
      {prior && (
        <section className="mt-4">
          <SectionHeading label={`The prior record of ${prior.key}`} level={3} count={prior.rows.length} />
          <ul className="mt-1 space-y-1 text-xs">
            {prior.rows.map((r, i) => (
              <li key={i} className="font-mono">
                {r.status} · {JSON.stringify(r.record)}
              </li>
            ))}
          </ul>
        </section>
      )}
      <ModalFooter>
        <Button type="secondary" htmlType="button" onClick={onClose}>
          Close
        </Button>
      </ModalFooter>
    </Modal>
  );
}

// ─── Profile schema ────────────────────────────────────────────────────

// A schema the server takes: 1-64 properties, each with a type and a description.
const PROFILE_EXAMPLE = JSON.stringify(
  { type: "object", properties: { timezone: { type: "string", description: "The user's IANA time zone" } } },
  null,
  2,
);

function ProfileSchemaPanel({ canSet, canClear }: { canSet: boolean; canClear: boolean }) {
  const identity = useIdentity();
  const { data, error, refresh } = useSection<ProfileSchema>((signal) => getProfileSchema(identity, { signal }));
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const shown = draft ?? (data?.found ? JSON.stringify(data.json_schema, null, 2) : "");
  let parseError: string | null = null;
  if (draft !== null && draft.trim()) {
    try {
      JSON.parse(draft);
    } catch {
      parseError = "Not valid JSON";
    }
  }

  const save = async (clear: boolean) => {
    if (clear && !(await confirmAction({ title: "Remove the profile schema?", danger: true }))) return;
    setBusy(true);
    try {
      if (clear) await clearProfileSchema(identity);
      else await setProfileSchema(identity, JSON.parse(shown));
      toast(clear ? "Profile schema removed" : "Profile schema saved", "ok");
      setDraft(null);
      refresh();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel>
      <SectionHeading label="Profile schema" level={2} caption="The JSON Schema a user's structured profile follows." />
      <SectionState error={error} loading={data === null} things="the profile schema" onRetry={refresh} />
      {data && (
        <div className="mt-2 space-y-2">
          <TextAreaField
            id="profile-schema"
            label="JSON Schema"
            value={shown}
            onChange={setDraft}
            rows={8}
            disabled={!canSet}
            placeholder={canSet ? PROFILE_EXAMPLE : "No profile schema."}
            error={parseError ?? undefined}
          />
          {data.updated_by && (
            <p className="text-xs text-text-muted">
              Set by {data.updated_by} {data.updated_at && <ListTime time={data.updated_at} quiet />}
            </p>
          )}
          <div className="flex justify-end gap-2">
            {canClear && data.found && (
              <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void save(true)}>
                Remove
              </Button>
            )}
            {canSet && (
              <Button
                type="primary"
                htmlType="button"
                disabled={busy || draft === null || !shown.trim() || !!parseError}
                onClick={() => void save(false)}
              >
                Save schema
              </Button>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}

// ─── Ontology ──────────────────────────────────────────────────────────

const KIND_LABEL: Record<OntologyKind, string> = {
  entity_type: "Entity type",
  relation_verb: "Relation",
  attribute: "Attribute",
};

function Ontology({ canRegister }: { canRegister: boolean }) {
  const identity = useIdentity();
  const { data, error, refresh } = useSection<OntologyEntry[]>((signal) => listOntology(identity, { signal }));
  const [kind, setKind] = useState<OntologyKind>("entity_type");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  // A registration replaces the entry, so a listed name is accepted from its row, not added again.
  const listed = data?.some((e) => e.kind === kind && e.name === name.trim()) ?? false;

  const accept = async (e: OntologyEntry) => {
    setBusy(true);
    try {
      await registerOntologyTerm(identity, e);
      toast(`Accepted ${e.name}`, "ok");
      refresh();
    } catch (err) {
      toast(`Failed: ${humanizeError(err)}`, "error");
    } finally {
      setBusy(false);
    }
  };
  const add = async () => {
    setBusy(true);
    try {
      await registerOntologyTerm(identity, { kind, name: name.trim(), description: "", cardinality: "" });
      toast(`Added ${name.trim()}`, "ok");
      setName("");
      refresh();
    } catch (err) {
      toast(`Failed: ${humanizeError(err)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel>
      <SectionHeading label="Graph ontology" level={2} caption="The entity types and relations the graph uses." />
      <SectionState error={error} loading={data === null} things="the ontology" onRetry={refresh} />
      {data?.length === 0 && <p className="mt-2 text-sm text-text-muted">No term registered.</p>}
      <ul className="mt-2 divide-y divide-border">
        {data?.map((e) => (
          <li key={`${e.kind}-${e.name}`} className="flex flex-wrap items-center gap-2 py-1.5">
            <span className="text-xs text-text-muted w-24">{KIND_LABEL[e.kind] ?? e.kind}</span>
            <span className="font-mono text-sm min-w-0 flex-1 truncate">{e.name}</span>
            {e.status === "pending" && <Badge text="pending" state="warning" size="sm" />}
            {canRegister && e.status === "pending" && (
              <Button
                type="secondary"
                htmlType="button"
                disabled={busy}
                aria-label={`Accept ${e.name}`}
                onClick={() => void accept(e)}
              >
                Accept
              </Button>
            )}
          </li>
        ))}
      </ul>
      {canRegister && (
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <div className="w-40">
            <Select
              value={kind}
              onSelect={(v) => setKind(v as OntologyKind)}
              triggerProps={{ "aria-label": "Kind of term" }}
            >
              {(Object.keys(KIND_LABEL) as OntologyKind[]).map((k) => (
                <Select.Item key={k} value={k}>
                  {KIND_LABEL[k]}
                </Select.Item>
              ))}
            </Select>
          </div>
          <TextField
            id="ontology-name"
            label="Name"
            value={name}
            onChange={setName}
            error={listed ? "Already listed" : undefined}
          />
          <Button
            type="secondary"
            htmlType="button"
            disabled={busy || !name.trim() || listed}
            onClick={() => void add()}
          >
            Add term
          </Button>
        </div>
      )}
    </Panel>
  );
}

// ─── Categories ────────────────────────────────────────────────────────

function Categories({ canAdd }: { canAdd: boolean }) {
  const identity = useIdentity();
  const { data, error, refresh } = useSection<Category[]>((signal) => listCategories(identity, { signal }));
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  // A category's id comes from its name, so adding a listed name updates that category.
  const updates = data?.some((c) => c.name === name.trim()) ?? false;
  const add = async () => {
    setBusy(true);
    try {
      await registerCategory(identity, name.trim(), description.trim());
      toast(`${updates ? "Updated" : "Added"} ${name.trim()}`, "ok");
      setName("");
      setDescription("");
      refresh();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel>
      <SectionHeading label="Categories" level={2} caption="The categories agents file memories under." />
      <SectionState error={error} loading={data === null} things="categories" onRetry={refresh} />
      {data?.length === 0 && <p className="mt-2 text-sm text-text-muted">No category yet.</p>}
      <ul className="mt-2 divide-y divide-border">
        {data?.map((c) => (
          <li key={c.category_id} className="flex items-center gap-2 py-1.5">
            <div className="min-w-0 flex-1">
              <div className="text-sm truncate">{c.name}</div>
              {c.description && <div className="text-xs text-text-muted truncate">{c.description}</div>}
            </div>
            <span className="text-xs text-text-muted tabular">{c.item_count} memories</span>
          </li>
        ))}
      </ul>
      {canAdd && (
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <TextField id="category-name" label="Name" value={name} onChange={setName} />
          <TextField id="category-description" label="Description" value={description} onChange={setDescription} />
          <Button type="secondary" htmlType="button" disabled={busy || !name.trim()} onClick={() => void add()}>
            {updates ? "Update category" : "Add category"}
          </Button>
        </div>
      )}
    </Panel>
  );
}
