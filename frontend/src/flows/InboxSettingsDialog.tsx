import { useEffect, useMemo, useRef, useState } from "react";
import { Inbox as InboxIcon, Plus, Settings as SettingsIcon, Trash2 } from "lucide-react";
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
import { humanizeError, relativeTime } from "../lib/format";
import { ApiError } from "../lib/errors";
import { toast } from "../components/Toast";
import { ModePill } from "../components/TaskPills";
import { CancelButton, DialogFrame, ErrorNote, Field, INPUT_CLS, PRIMARY_BTN, SECONDARY_BTN } from "./TaskDialogFrame";
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
      icon={editing ? <SettingsIcon size={16} /> : <InboxIcon size={16} />}
      description={
        editing
          ? `Owned by ${editing.owner_agent} on ${editing.owner_namespace}. Admins of the owner namespace edit these.`
          : `A queue for one agent, owned by ${identity.namespace}. Writers on this namespace pull from it; the trust table decides who else may post.`
      }
      width={720}
      footer={
        <>
          <CancelButton label="Close" />
          <button type="button" onClick={submit} disabled={!canSubmit} className={PRIMARY_BTN}>
            {busy ? "Saving…" : editing ? "Save settings" : "Register"}
          </button>
        </>
      }
    >
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Field id="inbox-name" label="Inbox" hint="its address; cannot change">
          <input
            id="inbox-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={!!editing}
            placeholder="triage-bot"
            className={`${INPUT_CLS} font-mono disabled:opacity-60`}
            autoFocus={!editing}
          />
          {nameError && <div className="mt-1 text-xs text-[var(--color-danger)]">{nameError}</div>}
        </Field>
        <Field id="inbox-owner" label="Owner agent" hint="the agent that pulls">
          <input
            id="inbox-owner"
            value={ownerAgent}
            onChange={(e) => setOwnerAgent(e.target.value)}
            placeholder="triage"
            className={`${INPUT_CLS} font-mono`}
          />
        </Field>
      </div>
      <Field id="inbox-description" label="Description">
        <input
          id="inbox-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          className={INPUT_CLS}
          placeholder="What lands here and who reads it"
        />
      </Field>
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
        <legend className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5">
          Accepted disclosure{" "}
          <span className="text-[var(--color-text-subtle)] font-normal">
            (a task the inbox does not accept is refused at create)
          </span>
        </legend>
        <div className="flex flex-wrap gap-4">
          {DISCLOSURES.map((d) => (
            <label key={d} className="inline-flex items-center gap-2 text-sm">
              <input type="checkbox" checked={accept.includes(d)} onChange={() => toggle(d)} /> {d}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <Field id="inbox-lease" label="Lease" hint="s">
          <input
            id="inbox-lease"
            type="number"
            min={30}
            max={86_400}
            value={lease}
            onChange={(e) => setLease(Number(e.target.value))}
            className={INPUT_CLS}
          />
        </Field>
        <Field id="inbox-attempts" label="Max attempts">
          <input
            id="inbox-attempts"
            type="number"
            min={1}
            max={20}
            value={maxAttempts}
            onChange={(e) => setMaxAttempts(Number(e.target.value))}
            className={INPUT_CLS}
          />
        </Field>
        <Field id="inbox-defers" label="Max defers">
          <input
            id="inbox-defers"
            type="number"
            min={1}
            max={50}
            value={maxDefers}
            onChange={(e) => setMaxDefers(Number(e.target.value))}
            className={INPUT_CLS}
          />
        </Field>
        <Field id="inbox-escalate" label="Escalate triage after" hint="s">
          <input
            id="inbox-escalate"
            type="number"
            min={60}
            max={30 * 86_400}
            value={escalate}
            onChange={(e) => setEscalate(Number(e.target.value))}
            className={INPUT_CLS}
          />
        </Field>
      </div>
      <Field id="inbox-webhook" label="Webhook URL" hint="a wake-up hint, never the delivery; optional">
        <input
          id="inbox-webhook"
          value={webhook}
          onChange={(e) => setWebhook(e.target.value)}
          className={`${INPUT_CLS} font-mono`}
          placeholder="https://"
        />
      </Field>
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

function PrincipalKindPill({ kind }: { kind: Principal["kind"] }) {
  const cls =
    kind === "human"
      ? "text-[var(--color-accent)] border-[var(--color-accent)]/40"
      : kind === "service_account"
        ? "text-[var(--color-text-muted)] border-[var(--color-border)]"
        : "text-[var(--color-text-subtle)] border-[var(--color-border)]";
  return (
    <span className={`inline-flex items-center text-2xs px-2 py-0.5 rounded-full border whitespace-nowrap ${cls}`}>
      {KIND_LABEL[kind]}
    </span>
  );
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
      <Field id="inbox-posters-filter" label="Posters" hint="chosen from the identities loom knows; never typed">
        <input
          id="inbox-posters-filter"
          type="search"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          disabled={!inbox}
          placeholder={inbox ? "Filter by name, subject or namespace" : "Name the inbox to choose posters"}
          className={`${INPUT_CLS} disabled:opacity-60`}
        />
      </Field>
      <div className="mt-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)]">
        {!inbox && (
          <div className="px-3 py-2 text-xs text-[var(--color-text-muted)]">Name the inbox to choose posters.</div>
        )}
        {inbox && loading && (
          <div className="px-3 py-2" role="status" aria-live="polite">
            <span className="sr-only">Loading identities…</span>
            <span
              aria-hidden="true"
              className="block h-3 w-2/3 rounded bg-[var(--color-border)] animate-pulse motion-reduce:animate-none!"
            />
          </div>
        )}
        {inbox && error && (
          <div className="px-3 py-2 text-xs text-[var(--color-danger)]" role="alert">
            Could not load identities: {error}
          </div>
        )}
        {inbox && known && rows.length === 0 && !loading && (
          <div className="px-3 py-2 text-xs text-[var(--color-text-muted)]">
            {q ? "No identity matches the filter." : "Loom knows nobody in this org yet."}
          </div>
        )}
        {rows.length > 0 && (
          <ul className="max-h-56 overflow-y-auto divide-y divide-[var(--color-border)]" aria-label="Posters">
            {rows.map((p) => {
              const checked = p.can_already_post || selected.includes(p.subject);
              const shown = p.namespaces.slice(0, NS_SHOWN);
              const more = p.namespaces.length - shown.length;
              return (
                <li key={p.subject}>
                  <label
                    className={`flex items-start gap-2.5 px-3 py-2 text-sm ${p.can_already_post ? "opacity-70 cursor-not-allowed" : "cursor-pointer hover:bg-[var(--color-surface)]"}`}
                  >
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={checked}
                      disabled={p.can_already_post}
                      onChange={() => toggle(p.subject)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        <span className="font-medium break-all">{p.label}</span>
                        {p.subject !== p.label && (
                          <span className="font-mono text-xs text-[var(--color-text-muted)] break-all">
                            {p.subject}
                          </span>
                        )}
                        <PrincipalKindPill kind={p.kind} />
                      </span>
                      <span className="block text-xs text-[var(--color-text-subtle)] break-words">
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
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="mt-1 text-xs text-[var(--color-text-subtle)]">
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
    <section aria-labelledby="inbox-trust-heading" className="pt-2 border-t border-[var(--color-border)] space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 id="inbox-trust-heading" className="section-label">
          Trust table{" "}
          <span className="text-[var(--color-text-subtle)] normal-case tracking-normal">
            · poster × kind → deny / review / auto
          </span>
        </h3>
        {!draft && (
          <button type="button" onClick={() => setDraft(EMPTY_DRAFT)} className={`${SECONDARY_BTN} text-xs px-2 py-1`}>
            <Plus size={12} /> Add rule
          </button>
        )}
      </div>
      {rows === null && <div className="text-xs text-[var(--color-text-muted)]">Loading…</div>}
      {rows && rows.length === 0 && !draft && (
        <div className="text-xs text-[var(--color-text-muted)]">
          No rules yet: writers on the owner namespace post as <span className="font-mono">auto</span>; anyone in the
          posters list lands in <span className="font-mono">review</span>.
        </div>
      )}
      {rows && rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-[var(--color-surface-2)] text-xs text-[var(--color-text-muted)]">
              <tr>
                <th className="text-left px-2 py-1.5 font-medium">Poster</th>
                <th className="text-left px-2 py-1.5 font-medium">Kind</th>
                <th className="text-left px-2 py-1.5 font-medium">Mode</th>
                <th className="text-left px-2 py-1.5 font-medium">Evidence</th>
                <th className="px-2 py-1.5">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={`${p.poster}::${p.kind}`} className="border-t border-[var(--color-border)]">
                  <td className="px-2 py-1.5 font-mono text-xs break-all">{p.poster}</td>
                  <td className="px-2 py-1.5 font-mono text-xs">{p.kind}</td>
                  <td className="px-2 py-1.5">
                    <ModePill mode={p.mode} />
                  </td>
                  <td className="px-2 py-1.5 font-mono text-xs">{p.require_evidence.join(", ") || "—"}</td>
                  <td className="px-2 py-1.5 whitespace-nowrap text-right">
                    <button
                      type="button"
                      onClick={() => edit(p)}
                      className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] underline mr-2"
                      disabled={busy}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => remove(p)}
                      className="text-xs text-danger hover:opacity-80 inline-flex items-center gap-1"
                      disabled={busy}
                      aria-label={`Remove rule for ${p.poster} × ${p.kind}`}
                    >
                      <Trash2 size={12} aria-hidden="true" /> Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {draft && (
        <div className="rounded-lg border border-[var(--color-border)] p-3 space-y-3 bg-[var(--color-surface-2)]">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <Field id="policy-poster" label="Poster" hint="subject, agent or *">
              <input
                id="policy-poster"
                value={draft.poster}
                onChange={(e) => setDraft({ ...draft, poster: e.target.value })}
                className={`${INPUT_CLS} font-mono`}
                placeholder="sa:reporter-1a2b3c"
                autoFocus
              />
            </Field>
            <Field id="policy-kind" label="Kind" hint="* for every kind">
              <input
                id="policy-kind"
                value={draft.kind}
                onChange={(e) => setDraft({ ...draft, kind: e.target.value })}
                className={`${INPUT_CLS} font-mono`}
              />
            </Field>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <Field id="policy-mode" label="Mode">
              <select
                id="policy-mode"
                aria-describedby="policy-mode-help"
                value={draft.mode}
                onChange={(e) => {
                  const mode = oneOf(MODES, e.target.value);
                  if (mode !== null) setDraft({ ...draft, mode });
                }}
                className={INPUT_CLS}
              >
                {MODES.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
              <div id="policy-mode-help" className="mt-1 text-xs text-[var(--color-text-subtle)]">
                {MODE_HELP}
              </div>
            </Field>
            <Field
              id="policy-evidence"
              label="Require evidence"
              hint="brief fields the poster must supply, comma-separated"
            >
              <input
                id="policy-evidence"
                value={draft.require_evidence}
                onChange={(e) => setDraft({ ...draft, require_evidence: e.target.value })}
                className={`${INPUT_CLS} font-mono`}
                placeholder="repro, evidence_url"
              />
            </Field>
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setDraft(null)} className={`${SECONDARY_BTN} text-xs`}>
              Discard
            </button>
            <button
              type="button"
              onClick={save}
              disabled={busy || !draft.poster.trim()}
              className={`${PRIMARY_BTN} text-xs`}
            >
              {busy ? "Saving…" : "Save rule"}
            </button>
          </div>
        </div>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
    </section>
  );
}
