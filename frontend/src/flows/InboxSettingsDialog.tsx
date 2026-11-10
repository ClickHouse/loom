import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { NumberField } from "@clickhouse/click-ui/NumberField";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
import { Table } from "@clickhouse/click-ui/Table";
import { TextField } from "@clickhouse/click-ui/TextField";
import { Title } from "@clickhouse/click-ui/Title";
import {
  listPolicies,
  listPrincipals,
  registerInbox,
  setPolicy,
  type Caller,
  type Disclosure,
  type Inbox,
  type PolicyMode,
  type PosterPolicy,
  type Principal,
} from "../api/tasks";
import { relativeTime } from "../lib/format";
import { humanizeError } from "../lib/errorWords";
import { ApiError } from "../lib/errors";
import { toast } from "../components/Toast";
import { ModePill } from "../components/TaskPills";
import { IconButton } from "../components/IconButton";
import { fieldError, Loading } from "../components/ui";
import { CancelButton, DialogFrame, ErrorNote } from "./TaskDialogFrame";
import { oneOf } from "../lib/oneOf";

/**
 * Register an inbox, or edit one. An inbox is one agent's queue: who may post into it,
 * which disclosure levels it accepts, how long a lease runs, how many attempts and defers
 * a task gets, and when a triage item escalates. Editing re-registers with
 * `if_match_version`.
 *
 * The posters list is never typed: it is picked from the identities loom
 * already knows (`inbox.principals` — people under their login subject,
 * bots under `sa:` subjects). Writers on the owner namespace post anyway,
 * so their rows are checked and disabled rather than listed.
 *
 * Below the settings sits the trust table: one row per poster × kind with a
 * mode (deny / review / auto) and the brief fields the poster must supply.
 * Loom stores only the mode; who accepts a task in review — the inbox's
 * agent or a person — is never loom's decision. Admin only.
 */
interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  identity: Caller;
  /** The inbox to edit; absent = register a new one. */
  inbox?: Inbox | null;
  onSaved: (inbox: Inbox) => void;
}

const DISCLOSURES: Disclosure[] = ["confidential", "internal", "public_ok"];
const MODES: PolicyMode[] = ["deny", "review", "auto"];
/** The mode is the whole rule: loom never decides acceptance itself. */
const MODE_HELP =
  "review: each task waits in triage until the inbox's agent or a person accepts it; auto: straight to the queue; deny: refused at create";

/** A field label with its muted hint, e.g. "Lease (s)". */
function hinted(label: string, hint: string): ReactNode {
  return (
    <>
      {label} <span className="font-normal text-text-muted">({hint})</span>
    </>
  );
}

function splitList(s: string): string[] {
  return s
    .split(/[\n,]/)
    .map((x) => x.trim())
    .filter(Boolean);
}

export function InboxSettingsDialog({ open, onOpenChange, identity, inbox, onSaved }: Props) {
  const editing = inbox ?? null;
  const [name, setName] = useState(editing?.inbox ?? "");
  const [ownerAgent, setOwnerAgent] = useState(editing?.owner_agent ?? "");
  const [description, setDescription] = useState(editing?.description ?? "");
  const [posters, setPosters] = useState<string[]>(editing?.posters ?? []);
  const [accept, setAccept] = useState<Disclosure[]>(editing?.accept_disclosure ?? [...DISCLOSURES]);
  const [lease, setLease] = useState(editing?.default_lease_seconds ?? 1800);
  const [maxAttempts, setMaxAttempts] = useState(editing?.max_attempts ?? 3);
  const [maxDefers, setMaxDefers] = useState(editing?.max_defers ?? 5);
  const [escalate, setEscalate] = useState(editing?.triage_escalate_after_seconds ?? 14_400);
  const [webhook, setWebhook] = useState(editing?.webhook_url ?? "");
  const [version, setVersion] = useState(editing?.version ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const nameError =
    name && !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(name)
      ? "Letters, digits, . _ : - ; starts with a letter or digit"
      : null;
  const canSubmit = !busy && name.trim() !== "" && ownerAgent.trim() !== "" && !nameError && accept.length > 0;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const r = await registerInbox(identity, {
        inbox: name.trim(),
        owner_agent: ownerAgent.trim(),
        description: description.trim(),
        posters,
        accept_disclosure: accept,
        default_lease_seconds: lease,
        max_attempts: maxAttempts,
        max_defers: maxDefers,
        triage_escalate_after_seconds: escalate,
        webhook_url: webhook.trim(),
        ...(version != null ? { if_match_version: version } : {}),
      });
      toast(r.created ? "Inbox registered" : "Inbox settings saved", "ok");
      setVersion(r.inbox.version);
      onSaved(r.inbox);
      if (r.created) onOpenChange(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.code === "PRECONDITION_FAILED") {
        const cv = Number(e.detail.current_version ?? 0);
        setError(
          `The inbox was changed by someone else (now version ${cv}). Save again to write over their version, or close and reopen to see it.`,
        );
        if (cv) setVersion(cv);
      } else {
        setError(humanizeError(e));
      }
    } finally {
      setBusy(false);
    }
  };

  const toggle = (d: Disclosure) => setAccept((cur) => (cur.includes(d) ? cur.filter((x) => x !== d) : [...cur, d]));

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? `Inbox settings — ${editing.inbox}` : "Register inbox"}
      description={
        editing
          ? `Owned by ${editing.owner_agent} on ${editing.owner_namespace}. Admins of the owner namespace edit these.`
          : `A queue for one agent, owned by ${identity.namespace}. Writers on this namespace pull from it; the trust table decides who else may post.`
      }
      width={720}
      footer={
        <>
          <CancelButton label="Close" />
          <Button type="primary" htmlType="button" onClick={submit} disabled={!canSubmit} loading={busy}>
            {editing ? "Save settings" : "Register"}
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <TextField
          id="inbox-name"
          label={hinted("Inbox", "its address; cannot change")}
          value={name}
          onChange={(value) => setName(value)}
          disabled={!!editing}
          placeholder="triage-bot"
          className="font-mono"
          autoFocus={!editing}
          {...fieldError("inbox-name-error", nameError)}
        />
        <TextField
          id="inbox-owner"
          label={hinted("Owner agent", "the agent that pulls")}
          value={ownerAgent}
          onChange={(value) => setOwnerAgent(value)}
          placeholder="triage"
          className="font-mono"
        />
      </div>
      <TextField
        id="inbox-description"
        label="Description"
        value={description}
        onChange={(value) => setDescription(value)}
        placeholder="What lands here and who reads it"
      />
      <PosterPicker
        identity={identity}
        open={open}
        // A new inbox has no address until it is named; the directory is
        // marked against the name once it is well-formed.
        inbox={editing ? editing.inbox : !nameError && name.trim() ? name.trim() : ""}
        debounceMs={editing ? 0 : 350}
        selected={posters}
        onChange={setPosters}
      />
      <fieldset>
        <legend className="mb-1.5 block text-xs font-medium text-text-muted">
          {hinted("Accepted disclosure", "a task the inbox does not accept is refused at create")}
        </legend>
        <div className="flex flex-wrap gap-4">
          {DISCLOSURES.map((d) => (
            <Checkbox key={d} label={d} checked={accept.includes(d)} onCheckedChange={() => toggle(d)} />
          ))}
        </div>
      </fieldset>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <NumberField
          id="inbox-lease"
          label={hinted("Lease", "s")}
          min={30}
          max={86_400}
          value={String(lease)}
          onChange={(value) => setLease(Number(value))}
        />
        <NumberField
          id="inbox-attempts"
          label="Max attempts"
          min={1}
          max={20}
          value={String(maxAttempts)}
          onChange={(value) => setMaxAttempts(Number(value))}
        />
        <NumberField
          id="inbox-defers"
          label="Max defers"
          min={1}
          max={50}
          value={String(maxDefers)}
          onChange={(value) => setMaxDefers(Number(value))}
        />
        <NumberField
          id="inbox-escalate"
          label={hinted("Escalate triage after", "s")}
          min={60}
          max={30 * 86_400}
          value={String(escalate)}
          onChange={(value) => setEscalate(Number(value))}
        />
      </div>
      <TextField
        id="inbox-webhook"
        label={hinted("Webhook URL", "a wake-up hint, never the delivery; optional")}
        type="url"
        value={webhook}
        onChange={(value) => setWebhook(value)}
        className="font-mono"
        placeholder="https://"
      />
      {error && <ErrorNote>{error}</ErrorNote>}

      {editing && <TrustTable identity={identity} inbox={editing.inbox} open={open} />}
    </DialogFrame>
  );
}

// ---------------------------------------------------------------- posters

const KIND_LABEL: Record<Principal["kind"], string> = {
  human: "Person",
  service_account: "Service account",
  other: "Other",
};
const NS_SHOWN = 3;
const CAN_POST_HINT = "already a writer here";

const KIND_STATE = { human: "info", service_account: "neutral", other: "default" } as const;

function PrincipalKindPill({ kind }: { kind: Principal["kind"] }) {
  return <Badge text={KIND_LABEL[kind]} state={KIND_STATE[kind]} size="sm" />;
}

/** A listed subject the directory did not return still needs a row so it
 *  can be unchecked; the server includes it, this is the belt to that brace. */
function placeholderRow(subject: string): Principal {
  return {
    subject,
    label: subject,
    kind: "other",
    namespaces: [],
    roles: [],
    last_seen: null,
    can_already_post: false,
    listed: true,
  };
}

function matches(p: Principal, q: string): boolean {
  if (!q) return true;
  return (
    p.label.toLowerCase().includes(q) ||
    p.subject.toLowerCase().includes(q) ||
    p.namespaces.some((n) => n.toLowerCase().includes(q))
  );
}

/**
 * The posters checklist. Rows come from `inbox.principals`, filtered on the
 * client (the directory is at most 200 rows); when the server reports a
 * truncated list the filter text is also sent as `query` after a short
 * debounce so the rows on screen are the ones that match. `selected` is the
 * posters array the dialog sends: listed rows seed it on the first load,
 * writers on the owner namespace never enter it (they post regardless).
 */
function PosterPicker({
  identity,
  open,
  inbox,
  debounceMs,
  selected,
  onChange,
}: {
  identity: Caller;
  open: boolean;
  /** The inbox the directory is marked against; "" until a new inbox is named. */
  inbox: string;
  debounceMs: number;
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const [known, setKnown] = useState<Map<string, Principal> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [filter, setFilter] = useState("");
  const [serverQuery, setServerQuery] = useState("");
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  // Listed rows seed the selection once per inbox; a later re-query (the
  // truncated case) must not re-check what the admin just unchecked.
  const seededFor = useRef<string | null>(null);

  useEffect(() => {
    if (!open || !inbox) return;
    let cancelled = false;
    const t = setTimeout(() => {
      setLoading(true);
      setError(null);
      listPrincipals(identity, { inbox, query: serverQuery })
        .then((r) => {
          if (cancelled) return;
          setKnown((cur) => {
            const next = new Map(cur ?? []);
            for (const p of r.principals) next.set(p.subject, p);
            return next;
          });
          setTruncated(r.truncated);
          const cannot = new Set(r.principals.filter((p) => p.can_already_post).map((p) => p.subject));
          const next = selectedRef.current.filter((s) => !cannot.has(s));
          if (seededFor.current !== inbox) {
            seededFor.current = inbox;
            for (const p of r.principals)
              if (p.listed && !p.can_already_post && !next.includes(p.subject)) next.push(p.subject);
          }
          if (next.length !== selectedRef.current.length || next.some((s, i) => s !== selectedRef.current[i]))
            onChangeRef.current(next);
        })
        .catch((e) => {
          if (!cancelled) setError(humanizeError(e));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, debounceMs);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [open, inbox, identity.org, identity.namespace, serverQuery, debounceMs]);

  // The server sees the filter only when it could not send everything.
  useEffect(() => {
    if (!truncated) return;
    const t = setTimeout(() => setServerQuery(filter.trim()), 300);
    return () => clearTimeout(t);
  }, [filter, truncated]);

  const q = filter.trim().toLowerCase();
  const rows = useMemo(() => {
    const all = known ? [...known.values()] : [];
    for (const s of selected) if (!known?.has(s)) all.push(placeholderRow(s));
    return all.filter((p) => matches(p, q));
  }, [known, selected, q]);

  const toggle = (subject: string) => {
    onChange(selected.includes(subject) ? selected.filter((s) => s !== subject) : [...selected, subject]);
  };

  return (
    <div>
      <div className="mb-1.5 text-xs font-medium text-text-muted">
        {hinted("Posters", "chosen from the identities loom knows; never typed")}
      </div>
      <SearchField
        id="inbox-posters-filter"
        aria-label="Filter posters"
        value={filter}
        onChange={(value) => setFilter(value)}
        disabled={!inbox}
        placeholder={inbox ? "Filter by name, subject or namespace" : "Name the inbox to choose posters"}
      />
      <div className="mt-2 rounded-lg border border-border bg-surface-2">
        {!inbox && <div className="px-3 py-2 text-xs text-text-muted">Name the inbox to choose posters.</div>}
        {inbox && loading && <Loading layout="inline" label="Loading identities" className="px-3 py-2" />}
        {inbox && error && (
          <div className="px-3 py-2 text-xs text-danger" role="alert">
            Could not load identities: {error}
          </div>
        )}
        {inbox && known && rows.length === 0 && !loading && (
          <div className="px-3 py-2 text-xs text-text-muted">
            {q ? "No identity matches the filter." : "Loom knows nobody in this org yet."}
          </div>
        )}
        {rows.length > 0 && (
          <ul className="max-h-56 divide-y divide-border overflow-y-auto" aria-label="Posters">
            {rows.map((p) => {
              const checked = p.can_already_post || selected.includes(p.subject);
              const shown = p.namespaces.slice(0, NS_SHOWN);
              const more = p.namespaces.length - shown.length;
              const detailsId = `inbox-poster-${p.subject}`;
              return (
                <li
                  key={p.subject}
                  className={`flex items-start gap-2.5 px-3 py-2 text-sm ${p.can_already_post ? "text-text-muted" : "hover:bg-surface"}`}
                >
                  <Checkbox
                    label={p.label}
                    aria-describedby={detailsId}
                    checked={checked}
                    disabled={p.can_already_post}
                    onCheckedChange={() => toggle(p.subject)}
                  />
                  <span id={detailsId} className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                      {p.subject !== p.label && (
                        <span className="break-all font-mono text-xs text-text-muted">{p.subject}</span>
                      )}
                      <PrincipalKindPill kind={p.kind} />
                    </span>
                    <span className="block break-words text-xs text-text-muted">
                      {shown.length > 0 ? (
                        <>
                          <span className="font-mono">{shown.join(", ")}</span>
                          {more > 0 && <span> +{more}</span>}
                        </>
                      ) : (
                        "no namespaces"
                      )}
                      {p.last_seen && <span> · last seen {relativeTime(p.last_seen)}</span>}
                      {p.can_already_post && <span className="italic"> · {CAN_POST_HINT}</span>}
                    </span>
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="mt-1 text-xs text-text-muted">
        {selected.length} listed as {selected.length === 1 ? "poster" : "posters"}
        {truncated && " · the directory was cut at the limit; the filter narrows it on the server"}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- trust table

interface Draft {
  poster: string;
  kind: string;
  mode: PolicyMode;
  require_evidence: string;
}
const EMPTY_DRAFT: Draft = { poster: "", kind: "*", mode: "review", require_evidence: "" };

function TrustTable({ identity, inbox, open }: { identity: Caller; inbox: string; open: boolean }) {
  const [rows, setRows] = useState<PosterPolicy[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);

  // Bumped after a save or remove to re-read the rules; a response for an inbox or
  // namespace the table has left is dropped instead of shown.
  const [reloadSeq, setReloadSeq] = useState(0);
  const load = () => setReloadSeq((n) => n + 1);
  useEffect(() => {
    if (!open) return;
    let live = true;
    listPolicies(identity, inbox)
      .then((r) => {
        if (live) setRows(r);
      })
      .catch((e) => {
        if (!live) return;
        setError(humanizeError(e));
        setRows([]);
      });
    return () => {
      live = false;
    };
    // identity is keyed by org and namespace: a new object for the same caller is no reason to re-read.
  }, [open, identity.org, identity.namespace, inbox, reloadSeq]);

  const edit = (p: PosterPolicy) =>
    setDraft({
      poster: p.poster,
      kind: p.kind,
      mode: p.mode,
      require_evidence: p.require_evidence.join(", "),
    });

  const save = async () => {
    if (!draft || !draft.poster.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await setPolicy(identity, {
        inbox,
        poster: draft.poster.trim(),
        kind: draft.kind.trim() || "*",
        mode: draft.mode,
        require_evidence: splitList(draft.require_evidence),
      });
      toast("Trust rule saved", "ok");
      setDraft(null);
      load();
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (p: PosterPolicy) => {
    setBusy(true);
    setError(null);
    try {
      await setPolicy(identity, { inbox, poster: p.poster, kind: p.kind, mode: p.mode, remove: true });
      toast("Trust rule removed", "ok");
      load();
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="inbox-trust-heading" className="space-y-3 border-t border-border pt-2">
      <div className="flex items-center justify-between gap-2">
        <Title type="h3" size="sm" id="inbox-trust-heading">
          Trust table <span className="font-normal text-text-muted">· poster × kind → deny / review / auto</span>
        </Title>
        {!draft && (
          <Button type="secondary" htmlType="button" iconLeft="plus" onClick={() => setDraft(EMPTY_DRAFT)}>
            Add rule
          </Button>
        )}
      </div>
      {rows === null && <Loading layout="inline" label="Loading trust rules" />}
      {rows && rows.length === 0 && !draft && (
        <div className="text-xs text-text-muted">
          No rules yet: writers on the owner namespace post as <span className="font-mono">auto</span>; anyone in the
          posters list lands in <span className="font-mono">review</span>.
        </div>
      )}
      {rows && rows.length > 0 && (
        <Table
          size="sm"
          headers={[
            { label: "Poster" },
            { label: "Kind" },
            { label: "Mode" },
            { label: "Evidence" },
            { label: <span className="sr-only">Actions</span>, width: "80px" },
          ]}
          rows={rows.map((p) => ({
            id: `${p.poster}::${p.kind}`,
            items: [
              { label: <span className="break-all font-mono text-xs">{p.poster}</span> },
              { label: <span className="font-mono text-xs">{p.kind}</span> },
              { label: <ModePill mode={p.mode} /> },
              { label: <span className="font-mono text-xs">{p.require_evidence.join(", ") || "—"}</span> },
              {
                label: (
                  <div className="flex justify-end gap-1">
                    <IconButton
                      icon="pencil"
                      size="sm"
                      label={`Edit rule for ${p.poster} × ${p.kind}`}
                      onClick={() => edit(p)}
                      disabled={busy}
                    />
                    <IconButton
                      icon="trash"
                      type="danger"
                      size="sm"
                      label={`Remove rule for ${p.poster} × ${p.kind}`}
                      onClick={() => remove(p)}
                      disabled={busy}
                    />
                  </div>
                ),
              },
            ],
          }))}
        />
      )}
      {draft && (
        <div className="space-y-3 rounded-lg border border-border bg-surface-2 p-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <TextField
              id="policy-poster"
              label={hinted("Poster", "subject, agent or *")}
              value={draft.poster}
              onChange={(value) => setDraft({ ...draft, poster: value })}
              className="font-mono"
              placeholder="sa:reporter-1a2b3c"
              autoFocus
            />
            <TextField
              id="policy-kind"
              label={hinted("Kind", "* for every kind")}
              value={draft.kind}
              onChange={(value) => setDraft({ ...draft, kind: value })}
              className="font-mono"
            />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <Select
                id="policy-mode"
                label="Mode"
                triggerProps={{ "aria-describedby": "policy-mode-help" }}
                value={draft.mode}
                onSelect={(value) => {
                  const mode = oneOf(MODES, value);
                  if (mode !== null) setDraft({ ...draft, mode });
                }}
                options={MODES.map((m) => ({ value: m, label: m }))}
              />
              <div id="policy-mode-help" className="mt-1 text-xs text-text-muted">
                {MODE_HELP}
              </div>
            </div>
            <TextField
              id="policy-evidence"
              label={hinted("Require evidence", "brief fields the poster must supply, comma-separated")}
              value={draft.require_evidence}
              onChange={(value) => setDraft({ ...draft, require_evidence: value })}
              className="font-mono"
              placeholder="repro, evidence_url"
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button type="secondary" htmlType="button" onClick={() => setDraft(null)}>
              Discard
            </Button>
            <Button type="secondary" htmlType="button" onClick={save} disabled={!draft.poster.trim()} loading={busy}>
              Save rule
            </Button>
          </div>
        </div>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
    </section>
  );
}
