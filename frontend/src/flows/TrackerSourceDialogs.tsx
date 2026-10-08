/**
 * Adding, checking, rotating and retiring a tracker from the page.
 *
 * A tracker is a ClickHouse database of a team's issues, PRs and incidents.
 * Who decides its schema: Loom decides the views it reads (fixed names and
 * columns); the team decides how its own tables map onto them, in its own
 * ClickHouse. So the flow starts with the schema, not the password:
 *
 *   1. Views — what Loom reads, table by table with what each column holds,
 *      and the view templates to copy or download (`tracker.contract`, no I/O).
 *   2. Connect — the address (a pasted console URL fills host, port, TLS and
 *      database), a read-only user, the repositories it holds; the name and
 *      access rules folded under "Advanced", defaulted.
 *   3. Check — `tracker.source.add(dry_run)`: every check, nothing written,
 *      each result beside the check it answers; a failing schema comes back
 *      with the fix per table. Add stays off until the form it would send is
 *      the one that passed.
 *   Then a done state that says what happened and what happens next.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Circle,
  Database,
  Download,
  Info,
  KeyRound,
  ShieldAlert,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import {
  addTrackerSource,
  getTrackerContract,
  inspectTrackerSource,
  removeTrackerSource,
  rotateTrackerSource,
  setTrackerMapping,
} from "../api";
import { ApiError } from "../lib/errors";
import { humanizeError } from "../lib/format";
import {
  CAPABILITY_LABEL,
  contractRows,
  defaultNamespaceFor,
  formFingerprint,
  mappingProblems,
  parseClickHouseUrl,
  sourceAddBody,
  sourceFormProblems,
  templatesFile,
  type SourceMapping,
  type TrackerInspectResult,
  type ContractTable,
  type ContractTableRow,
  type TrackerContractCatalogue,
  type TrackerContractReport,
  type TrackerSourceAddResult,
  type TrackerSourceForm,
} from "../lib/trackerSources";
import { CopyButton } from "../components/CopyButton";
import { Skeleton } from "../components/Skeleton";
import { MappingEditor } from "./TrackerMapping";
import {
  CancelButton,
  DANGER_BTN,
  DialogFrame,
  ErrorNote,
  Field,
  INPUT_CLS,
  PRIMARY_BTN,
  SECONDARY_BTN,
} from "./TaskDialogFrame";

// ---------------------------------------------------------------------------
// The check report, table by table (shared with a tracker's "Check now")
// ---------------------------------------------------------------------------

const STATE_ICON: Record<ContractTableRow["state"], { icon: typeof CheckCircle2; tone: string; word: string }> = {
  ok: { icon: CheckCircle2, tone: "text-success", word: "ok" },
  aliased: { icon: CheckCircle2, tone: "text-success", word: "ok" },
  incomplete: { icon: AlertTriangle, tone: "text-warn", word: "incomplete" },
  missing: { icon: XCircle, tone: "text-danger", word: "missing" },
  absent: { icon: Info, tone: "text-text-muted", word: "absent" },
};

/** The schema check, one row per table, and a copyable fix per failing table. */
export function ContractReportView({
  report,
  templates,
}: {
  report: TrackerContractReport;
  templates?: Record<string, string>;
}) {
  const rows = contractRows(report);
  const drafts = Object.entries(templates ?? {});
  const all = drafts.map(([, sql]) => sql).join("\n\n");
  return (
    <div className="space-y-3">
      <ul className="space-y-1" aria-label="Views, table by table">
        {rows.map((r) => {
          const s = STATE_ICON[r.state];
          const Icon = s.icon;
          return (
            <li key={r.table} className="flex items-start gap-2 text-xs min-w-0">
              <Icon size={13} aria-hidden="true" className={`${s.tone} shrink-0 mt-0.5`} />
              <span className="font-mono text-text shrink-0">{r.table}</span>
              <span className="sr-only">{s.word}</span>
              <span className="text-text-muted min-w-0 break-words">
                {!r.required && <span className="text-text-subtle">optional · </span>}
                {r.detail}
              </span>
            </li>
          );
        })}
      </ul>
      {drafts.length > 0 && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-text-muted min-w-0">
              Replace these views in <span className="font-mono">{report.database || "the database"}</span>: set{" "}
              <span className="font-mono">&lt;your_table&gt;</span>, map every column marked{" "}
              <strong className="font-medium">MUST map</strong>, run it, then Check again.
            </p>
            {drafts.length > 1 && (
              <CopyButton text={all} label="Copy all fixes" className={`${SECONDARY_BTN} text-xs`} />
            )}
          </div>
          {drafts.map(([table, sql]) => (
            <SqlBlock key={table} table={table} sql={sql} copyLabel={`Copy the view draft for ${table}`} />
          ))}
        </div>
      )}
    </div>
  );
}

function SqlBlock({ table, sql, copyLabel }: { table: string; sql: string; copyLabel: string }) {
  return (
    <div className="rounded-md border border-border bg-surface-2 min-w-0">
      <div className="flex items-center justify-between px-2 py-1 border-b border-border">
        <span className="text-xs font-mono">{table}</span>
        <CopyButton
          text={sql}
          label="Copy SQL"
          ariaLabel={copyLabel}
          className="inline-flex items-center gap-1 px-2 min-h-7 text-xs rounded-md border border-border hover:bg-surface-3"
        />
      </div>
      <pre className="max-h-56 overflow-auto p-2 text-2xs leading-relaxed font-mono whitespace-pre">{sql}</pre>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add a tracker — views, connect, check
// ---------------------------------------------------------------------------

type Step = "connect" | "map" | "check" | "done";
const STEPS: { id: Exclude<Step, "done">; label: string }[] = [
  { id: "connect", label: "Connect" },
  { id: "map", label: "Map" },
  { id: "check", label: "Check" },
];

const EMPTY: TrackerSourceForm = {
  name: "",
  host: "",
  port: 8443,
  secure: true,
  database: "",
  user: "",
  password: "",
  namespace: "",
  repos: [],
  restricted: false,
  join_policy: "open",
};

/** The gate a refusal belongs to, by its code, so it lands beside that check. */
type Gate = "reach" | "readonly" | "schema" | "rows" | "name" | "other";
function gateOf(code: string): Gate {
  if (code === "HOST_REFUSED" || code === "SOURCE_UNREACHABLE") return "reach";
  // Readonly = 1 is reachable but refuses loom's per-query settings
  if (code === "CREDENTIAL_NOT_READONLY" || code === "CREDENTIAL_READONLY_MODE") return "readonly";
  if (code === "CONTRACT_FAILED" || code === "MAPPING_FAILED" || code === "BAD_MAPPING") return "schema";
  // The columns match, but no row can be read through the views
  if (code === "VIEWS_NOT_READABLE") return "rows";
  if (["SOURCE_EXISTS", "NAMESPACE_TAKEN", "BAD_NAME", "BAD_NAMESPACE"].includes(code)) return "name";
  return "other";
}

/** The user a team creates for Loom (mirrors onboarding.READONLY_USER_SQL). */
function readonlyUserSql(database: string): string {
  return [
    "CREATE USER loom_ro IDENTIFIED BY '<password>' SETTINGS readonly = 2;",
    `GRANT SELECT ON ${database}.* TO loom_ro;`,
    "GRANT SELECT ON system.columns TO loom_ro;",
  ].join("\n");
}

/** A free name from the first repository: `ClickHouse/kj` → `kj`, `kj-2` when taken. */
export function proposeName(repos: readonly string[], taken: ReadonlySet<string>): string {
  const base = (repos[0]?.split("/").pop() ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .slice(0, 28);
  if (!base || base.length < 2) return "";
  if (!taken.has(base)) return base;
  for (let i = 2; i < 100; i++) if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
  return "";
}

export function RegisterTrackerSourceDialog({
  open,
  onOpenChange,
  org,
  repo,
  takenNames = [],
  takenNamespaces = [],
  onRegistered,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  /** The repository the dialog was opened from; the tracker will list it and be linked to it. */
  repo?: string;
  /** Names and namespaces already in use, so the defaults are free. */
  takenNames?: string[];
  takenNamespaces?: string[];
  /** After a real add: the namespace written (the caller links it). */
  onRegistered: (namespace: string, result: TrackerSourceAddResult) => void;
}) {
  const taken = useMemo(
    () => new Set([...takenNames, ...takenNamespaces.map((n) => n.replace(/^(?:tracker|qa)-/, ""))]),
    [takenNames, takenNamespaces],
  );
  const initialRepos = repo ? [repo] : [];
  const initialName = proposeName(initialRepos, taken);
  const [step, setStep] = useState<Step>("connect");
  const [form, setForm] = useState<TrackerSourceForm>({
    ...EMPTY,
    repos: initialRepos,
    name: initialName,
    namespace: defaultNamespaceFor(initialName),
  });
  const [address, setAddress] = useState("");
  const [touched, setTouched] = useState({ name: false, namespace: false });
  const [busy, setBusy] = useState<"inspect" | "check" | "add" | null>(null);
  const [refusal, setRefusal] = useState<{ gate: Gate; message: string } | null>(null);
  const [checked, setChecked] = useState<{ fp: string; result: TrackerSourceAddResult } | null>(null);
  const [added, setAdded] = useState<TrackerSourceAddResult | null>(null);
  // What loom read of the database, and how it maps
  const [inspected, setInspected] = useState<TrackerInspectResult | null>(null);
  const [mode, setMode] = useState<"mapping" | "views">("mapping");
  const [mapping, setMapping] = useState<SourceMapping>({ version: 1, tables: {} });

  const problems = useMemo(() => sourceFormProblems(form), [form]);
  const mapProblems = mode === "mapping" ? mappingProblems(mapping) : [];
  const fp = formFingerprint(form) + "|" + mode + "|" + (mode === "mapping" ? JSON.stringify(mapping) : "");
  const passed = checked !== null && checked.fp === fp && checked.result.contract.ok === true;
  const stale = checked !== null && checked.fp !== fp;

  const set = <K extends keyof TrackerSourceForm>(k: K, v: TrackerSourceForm[K]) => {
    setForm((f) => {
      const next: TrackerSourceForm = { ...f, [k]: v };
      if (k === "repos" && !touched.name) next.name = proposeName(next.repos, taken);
      if ((k === "name" || k === "repos") && !touched.namespace)
        next.namespace = defaultNamespaceFor(next.name) + (next.restricted ? "-private" : "");
      if (k === "restricted" && !touched.namespace)
        next.namespace = defaultNamespaceFor(next.name) + (v ? "-private" : "");
      return next;
    });
  };

  const onAddress = (text: string) => {
    setAddress(text);
    const p = parseClickHouseUrl(text);
    if (!p) {
      set("host", "");
      return;
    }
    setForm((f) => ({
      ...f,
      host: p.host,
      port: p.port ?? f.port,
      secure: p.secure ?? f.secure,
      database: p.database ?? f.database,
      user: p.user ?? f.user,
    }));
  };

  const inspect = async () => {
    if (problems.length) return;
    setBusy("inspect");
    setRefusal(null);
    try {
      const out = await inspectTrackerSource({
        org,
        host: form.host,
        port: form.port,
        secure: form.secure,
        database: form.database.trim(),
        user: form.user.trim(),
        password: form.password,
        repos: form.repos,
      });
      setInspected(out);
      setMode(out.proposal.mode);
      if (out.proposal.mapping) setMapping(out.proposal.mapping);
      setStep("map");
    } catch (e) {
      const code = e instanceof ApiError ? e.code : "";
      setRefusal({ gate: gateOf(code), message: humanizeError(e) });
    } finally {
      setBusy(null);
    }
  };

  const run = async (dryRun: boolean) => {
    if (problems.length || mapProblems.length) return;
    setBusy(dryRun ? "check" : "add");
    setRefusal(null);
    try {
      const out = await addTrackerSource(sourceAddBody(org, form, dryRun, mode === "mapping" ? mapping : null));
      if (dryRun) {
        setChecked({ fp, result: out });
      } else {
        setAdded(out);
        setStep("done");
        onRegistered(out.namespaces[0]?.namespace ?? form.namespace, out);
      }
    } catch (e) {
      const code = e instanceof ApiError ? e.code : "";
      setRefusal({ gate: gateOf(code), message: humanizeError(e) });
      if (dryRun) setChecked(null);
    } finally {
      setBusy(null);
    }
  };

  const goCheck = () => {
    setStep("check");
    void run(true);
  };

  const footer = (() => {
    if (step === "connect")
      return (
        <>
          <CancelButton />
          <button
            type="button"
            className={PRIMARY_BTN}
            disabled={problems.length > 0 || busy !== null}
            onClick={() => void inspect()}
            title={problems.length ? problems[0] : undefined}
          >
            {busy === "inspect" ? (
              "Reading your tables…"
            ) : (
              <>
                Next: map your tables <ChevronRight size={14} aria-hidden="true" />
              </>
            )}
          </button>
        </>
      );
    if (step === "map")
      return (
        <>
          <button type="button" className={SECONDARY_BTN} onClick={() => setStep("connect")}>
            Back
          </button>
          <button
            type="button"
            className={PRIMARY_BTN}
            disabled={mapProblems.length > 0 || busy !== null}
            onClick={goCheck}
            title={mapProblems[0]}
          >
            Check <ChevronRight size={14} aria-hidden="true" />
          </button>
        </>
      );
    if (step === "check")
      return (
        <>
          <button
            type="button"
            className={SECONDARY_BTN}
            disabled={busy !== null}
            onClick={() => setStep(inspected ? "map" : "connect")}
          >
            {inspected ? "Edit mapping" : "Edit connection"}
          </button>
          <button
            type="button"
            className={SECONDARY_BTN}
            disabled={busy !== null || problems.length > 0}
            onClick={() => void run(true)}
          >
            {busy === "check" ? "Checking…" : "Check again"}
          </button>
          <button
            type="button"
            className={PRIMARY_BTN}
            disabled={busy !== null || !passed}
            onClick={() => void run(false)}
          >
            {busy === "add" ? "Adding…" : repo ? `Add and link ${repo}` : "Add tracker"}
          </button>
        </>
      );
    return (
      <button type="button" className={PRIMARY_BTN} onClick={() => onOpenChange(false)}>
        Done
      </button>
    );
  })();

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={760}
      icon={<Database size={16} aria-hidden="true" />}
      title="Add a tracker"
      description="A tracker is a ClickHouse database of your issues, PRs and incidents — in whatever schema you have. Loom reads it live with a read-only user and never copies it."
      footer={footer}
    >
      {step !== "done" && (
        <Stepper
          step={step}
          canGo={(s) =>
            s === "connect" ||
            (s === "map" && inspected !== null) ||
            (s === "check" && (checked !== null || refusal !== null))
          }
          onStep={setStep}
        />
      )}
      {step === "connect" && (
        <>
          <ConnectStep
            form={form}
            address={address}
            onAddress={onAddress}
            set={set}
            problems={problems}
            onTouch={(k) => setTouched((t) => ({ ...t, [k]: true }))}
          />
          {refusal && <ErrorNote>{refusal.message}</ErrorNote>}
        </>
      )}
      {step === "map" && inspected && (
        <MapStep
          org={org}
          form={form}
          inspected={inspected}
          mode={mode}
          onMode={setMode}
          mapping={mapping}
          onMapping={setMapping}
          problems={mapProblems}
        />
      )}
      {step === "check" && (
        <CheckStep
          form={form}
          busy={busy === "check"}
          refusal={refusal}
          checked={checked?.result ?? null}
          stale={stale}
        />
      )}
      {step === "done" && added && <DoneStep added={added} repo={repo} />}
    </DialogFrame>
  );
}

function MapStep({
  org,
  form,
  inspected,
  mode,
  onMode,
  mapping,
  onMapping,
  problems,
}: {
  org: string;
  form: TrackerSourceForm;
  inspected: TrackerInspectResult;
  mode: "mapping" | "views";
  onMode: (m: "mapping" | "views") => void;
  mapping: SourceMapping;
  onMapping: (m: SourceMapping) => void;
  problems: string[];
}) {
  const [views, setViews] = useState(false);
  const db = form.database.trim();
  if (inspected.proposal.mode === "views") {
    return (
      <div className="rounded-lg bg-surface-2 border border-border p-3 text-sm space-y-1" role="status">
        <p className="flex items-center gap-1.5">
          <CheckCircle2 size={14} className="text-success" aria-hidden="true" />{" "}
          <span>
            <span className="font-mono">{db}</span> already exposes Loom's tables — nothing to map.
          </span>
        </p>
        <p className="text-xs text-text-muted">
          Loom reads <span className="font-mono">github_issues</span>, <span className="font-mono">github_prs</span> and{" "}
          <span className="font-mono">edges</span> there as they are. Go to Check.
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <div className="rounded-lg bg-surface-2 border border-border p-3 text-sm space-y-1">
        <p>
          <strong className="font-medium">Your schema, not Loom's:</strong> pick the table that holds your items and
          which of its columns holds each field. Loom proposed what it could match — check the ones marked "guessed".
          Fields you leave on "Loom default" are filled in for you.
        </p>
        <p className="text-xs text-text-muted">
          Loom reads {inspected.tables.length} table{inspected.tables.length === 1 ? "" : "s"} in{" "}
          <span className="font-mono">{db}</span>. Its read-only user needs SELECT on the tables you map.
        </p>
      </div>
      {mode === "mapping" && (
        <MappingEditor
          database={db}
          tables={inspected.tables}
          mapping={mapping}
          proposal={inspected.proposal}
          onChange={onMapping}
        />
      )}
      {problems.length > 0 && (
        <ul className="text-xs text-warn space-y-0.5" aria-label="Before the check">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      <details
        className="group rounded-lg border border-border"
        open={views}
        onToggle={(e) => setViews(e.currentTarget.open)}
      >
        <summary className="cursor-pointer list-none px-3 min-h-10 flex items-center gap-1.5 text-sm">
          <ChevronRight size={13} aria-hidden="true" className="transition-transform group-open:rotate-90" />
          Prefer views?{" "}
          <span className="text-xs text-text-muted">— create Loom's tables in your database yourself</span>
        </summary>
        <div className="px-3 pb-3 space-y-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={mode === "views"}
              onChange={(e) => onMode(e.target.checked ? "views" : "mapping")}
            />
            I created Loom's views in <span className="font-mono">{db}</span> — read them instead of a mapping
          </label>
          {views && <ViewsStep org={org} database={db} onDatabase={() => {}} />}
        </div>
      </details>
    </div>
  );
}

function Stepper({
  step,
  canGo,
  onStep,
}: {
  step: Exclude<Step, "done">;
  /** A step with something to show: Views and Connect always, Check once it ran. */
  canGo: (s: Exclude<Step, "done">) => boolean;
  onStep: (s: Exclude<Step, "done">) => void;
}) {
  const at = STEPS.findIndex((s) => s.id === step);
  return (
    <ol className="flex items-center gap-2 text-xs" aria-label="Steps">
      {STEPS.map((s, i) => {
        const state = i < at ? "done" : i === at ? "current" : "todo";
        return (
          <li key={s.id} className="flex items-center gap-2 min-w-0">
            <button
              type="button"
              onClick={() => onStep(s.id)}
              aria-current={state === "current" ? "step" : undefined}
              disabled={state === "current" || !canGo(s.id)}
              className={`inline-flex items-center gap-1.5 rounded-full px-2.5 min-h-8 border ${state === "current" ? "border-accent bg-accent-soft text-text font-medium" : state === "done" ? "border-border text-text" : "border-border text-text-subtle"} disabled:cursor-default`}
            >
              {state === "done" ? (
                <CheckCircle2 size={12} className="text-success" aria-hidden="true" />
              ) : (
                <span className="tabular-nums" aria-hidden="true">
                  {i + 1}
                </span>
              )}
              <span aria-hidden="true">{s.label}</span>
              <span className="sr-only">{`Step ${i + 1} of ${STEPS.length}: ${s.label}${state === "done" ? " (done)" : ""}`}</span>
            </button>
            {i < STEPS.length - 1 && (
              <ChevronRight size={12} aria-hidden="true" className="text-text-subtle shrink-0" />
            )}
          </li>
        );
      })}
    </ol>
  );
}

// ---- 1. Views --------------------------------------------------------------

function ViewsStep({ org, database, onDatabase }: { org: string; database: string; onDatabase: (v: string) => void }) {
  const [cat, setCat] = useState<TrackerContractCatalogue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [db, setDb] = useState(database);
  // the templates name the database; re-read when it settles (the op is pure and cheap)
  useEffect(() => {
    const ctl = new AbortController();
    const t = setTimeout(
      () => {
        getTrackerContract(org, db.trim(), { signal: ctl.signal })
          .then((c) => {
            setCat(c);
            setError(null);
          })
          .catch((e) => {
            if (!ctl.signal.aborted) setError(humanizeError(e));
          });
      },
      cat ? 300 : 0,
    );
    return () => {
      ctl.abort();
      clearTimeout(t);
    };
  }, [org, db]);
  const required = cat?.tables.filter((t) => t.required) ?? [];
  const optional = cat?.tables.filter((t) => !t.required) ?? [];
  const download = (tables: string[], name: string) => {
    if (!cat) return;
    const blob = new Blob([templatesFile(cat, tables)], { type: "text/sql" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <div className="space-y-4">
      <div className="rounded-lg bg-surface-2 border border-border p-3 text-sm space-y-1">
        <p>
          <strong className="font-medium">Who decides the schema:</strong> Loom reads fixed view names and columns; you
          decide how your tables map onto them, as views in your own ClickHouse. Loom only ever reads.
        </p>
        <p className="text-xs text-text-muted">
          Create the three required views below, then connect. Already have them? Go straight to Next.
        </p>
      </div>
      <Field id="views-db" label="Database for the views" hint="create it if it does not exist">
        <input
          id="views-db"
          className={`${INPUT_CLS} font-mono`}
          value={db}
          placeholder="loom_tracker"
          onChange={(e) => {
            setDb(e.target.value);
            onDatabase(e.target.value);
          }}
        />
      </Field>
      {error && (
        <ErrorNote>
          Couldn't load the view list — {error}. You can still connect; the check lists what is missing.
        </ErrorNote>
      )}
      {!cat && !error && (
        <div className="space-y-2" role="status" aria-label="Loading the views">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}
      {cat && (
        <>
          <section aria-label="Required views" className="space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-medium">Required — Loom does not read the tracker without these</h3>
              <div className="flex flex-wrap gap-2">
                <CopyButton
                  text={() =>
                    templatesFile(
                      cat,
                      required.map((t) => t.table),
                    )
                  }
                  label="Copy SQL"
                  className={`${SECONDARY_BTN} text-xs`}
                  ariaLabel="Copy the SQL for the required views"
                />
                <button
                  type="button"
                  className={`${SECONDARY_BTN} text-xs`}
                  onClick={() =>
                    download(
                      required.map((t) => t.table),
                      "loom-tracker-views.sql",
                    )
                  }
                >
                  <Download size={12} aria-hidden="true" /> Download .sql
                </button>
              </div>
            </div>
            {required.map((t) => (
              <TableCard key={t.table} t={t} defaultOpen={false} />
            ))}
            <p className="text-xs text-text-muted">
              The templates create <span className="font-mono">DEFINER</span> views (they read with your rights, so
              Loom's user needs access to the views only) and a <span className="font-mono">loom_uuid5</span> function
              that makes each item's <span className="font-mono">id</span> from its{" "}
              <span className="font-mono">source_id</span> — the same id Loom computes. Keep both.
            </p>
          </section>
          <section aria-label="Read-only user" className="space-y-2">
            <h3 className="text-sm font-medium">A read-only user for Loom</h3>
            <p className="text-xs text-text-muted">
              <span className="font-mono">readonly = 2</span>, not 1: it still allows SELECT only, but lets Loom set a
              timeout on each read. It needs SELECT on the views and on{" "}
              <span className="font-mono">system.columns</span> — nothing on your tables.
            </p>
            <SqlBlock
              table="read-only user"
              sql={readonlyUserSql(db.trim() || "loom_tracker")}
              copyLabel="Copy the SQL for the read-only user"
            />
          </section>
          <details className="group rounded-lg border border-border">
            <summary className="cursor-pointer list-none px-3 min-h-10 flex items-center gap-1.5 text-sm">
              <ChevronRight size={13} aria-hidden="true" className="transition-transform group-open:rotate-90" />
              Optional views ({optional.length}){" "}
              <span className="text-xs text-text-muted">
                — incidents, checks, releases; add them when you have the data
              </span>
            </summary>
            <div className="px-3 pb-3 space-y-2">
              <button
                type="button"
                className={`${SECONDARY_BTN} text-xs`}
                onClick={() =>
                  download(
                    cat.tables.map((t) => t.table),
                    "loom-tracker-views-all.sql",
                  )
                }
              >
                <Download size={12} aria-hidden="true" /> Download all {cat.tables.length} views
              </button>
              {optional.map((t) => (
                <TableCard key={t.table} t={t} defaultOpen={false} />
              ))}
            </div>
          </details>
        </>
      )}
    </div>
  );
}

function TableCard({ t, defaultOpen }: { t: ContractTable; defaultOpen: boolean }) {
  const must = t.columns.filter((c) => c.must_map).length;
  return (
    <details className="group rounded-lg border border-border" open={defaultOpen}>
      <summary className="cursor-pointer list-none px-3 py-2 flex flex-wrap items-center gap-x-2 gap-y-0.5 min-h-10">
        <ChevronRight size={13} aria-hidden="true" className="transition-transform group-open:rotate-90 shrink-0" />
        <span className="font-mono text-sm">{t.table}</span>
        <span className="text-xs text-text-muted">{t.note}</span>
        <span className="text-2xs text-text-subtle ml-auto">
          {t.columns.length} column{t.columns.length === 1 ? "" : "s"}
          {must ? ` · ${must} must map` : ""}
        </span>
      </summary>
      <div className="px-3 pb-3 overflow-x-auto">
        <table className="w-full text-xs">
          <caption className="sr-only">Columns of {t.table}</caption>
          <thead>
            <tr className="text-left text-text-subtle">
              <th className="font-normal py-1 pr-3">column</th>
              <th className="font-normal py-1 pr-3">type</th>
              <th className="font-normal py-1">holds</th>
            </tr>
          </thead>
          <tbody>
            {t.columns.map((c) => (
              <tr key={c.name} className="border-t border-border align-top">
                <td className="py-1 pr-3 font-mono whitespace-nowrap">
                  {c.name}
                  {c.must_map && (
                    <span className="ml-1.5 rounded bg-warn/15 text-warn px-1 text-2xs font-sans">must map</span>
                  )}
                </td>
                <td className="py-1 pr-3 font-mono text-text-muted whitespace-nowrap">{c.type}</td>
                <td className="py-1 text-text-muted">{c.note || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

// ---- 2. Connect ------------------------------------------------------------

function RepoChips({ repos, onChange }: { repos: string[]; onChange: (r: string[]) => void }) {
  const [draft, setDraft] = useState("");
  const commit = (text: string) => {
    const add = text
      .split(/[\s,]+/)
      .map((x) => x.trim())
      .filter(Boolean);
    if (add.length) onChange([...new Set([...repos, ...add])]);
    setDraft("");
  };
  return (
    <div className={`${INPUT_CLS} flex flex-wrap items-center gap-1.5 min-h-10 py-1`}>
      {repos.map((r) => (
        <span
          key={r}
          className="inline-flex items-center gap-1 rounded-full bg-surface-3 px-2 min-h-7 text-xs font-mono"
        >
          {r}
          <button
            type="button"
            aria-label={`Remove ${r}`}
            className="rounded-full p-0.5 hover:bg-surface-2 min-h-6 min-w-6 inline-flex items-center justify-center"
            onClick={() => onChange(repos.filter((x) => x !== r))}
          >
            <X size={11} aria-hidden="true" />
          </button>
        </span>
      ))}
      <input
        id="tsrc-repos"
        aria-label="Repositories it holds"
        className="flex-1 min-w-32 bg-transparent outline-none font-mono text-sm"
        value={draft}
        placeholder={repos.length ? "add another…" : "Org/repo"}
        onChange={(e) => {
          const v = e.target.value;
          if (/[\s,]$/.test(v)) commit(v);
          else setDraft(v);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit(draft);
          } else if (e.key === "Backspace" && !draft && repos.length) onChange(repos.slice(0, -1));
        }}
        onBlur={() => commit(draft)}
      />
    </div>
  );
}

function ConnectStep({
  form,
  address,
  onAddress,
  set,
  problems,
  onTouch,
}: {
  form: TrackerSourceForm;
  address: string;
  onAddress: (v: string) => void;
  set: <K extends keyof TrackerSourceForm>(k: K, v: TrackerSourceForm[K]) => void;
  problems: string[];
  onTouch: (k: "name" | "namespace") => void;
}) {
  const [advanced, setAdvanced] = useState(false);
  const nameProblem = problems.find((p) => p.startsWith("Name:") || p.startsWith("Namespace:"));
  const id = (s: string) => `tsrc-${s}`;
  return (
    <div className="space-y-4">
      <details className="group rounded-lg border border-border">
        <summary className="cursor-pointer list-none px-3 min-h-10 flex items-center gap-1.5 text-sm">
          <ChevronRight size={13} aria-hidden="true" className="transition-transform group-open:rotate-90" />
          Need a read-only user? <span className="text-xs text-text-muted">— run this in your ClickHouse</span>
        </summary>
        <div className="px-3 pb-3 space-y-1">
          <p className="text-xs text-text-muted">
            <span className="font-mono">readonly = 2</span>, not 1: SELECT only, and Loom can still set a timeout on
            each read.
          </p>
          <SqlBlock
            table="read-only user"
            sql={readonlyUserSql(form.database.trim() || "<your_database>")}
            copyLabel="Copy the SQL for the read-only user"
          />
        </div>
      </details>
      <Field id={id("addr")} label="ClickHouse address" hint="paste the URL from your console, or a host">
        <input
          id={id("addr")}
          className={`${INPUT_CLS} font-mono`}
          value={address}
          autoFocus
          placeholder="https://abc.us-east-1.aws.clickhouse.cloud:8443"
          onChange={(e) => onAddress(e.target.value)}
        />
      </Field>
      {form.host && (
        <div className="flex flex-wrap items-end gap-3 text-xs">
          <span className="text-text-muted">
            Host <span className="font-mono text-text">{form.host}</span>
          </span>
          <label className="inline-flex items-center gap-1.5 text-text-muted">
            Port
            <input
              className="min-h-8 w-20 rounded-md border border-border bg-surface-2 px-2 font-mono text-text"
              inputMode="numeric"
              value={String(form.port)}
              onChange={(e) => set("port", Number(e.target.value.replace(/\D/g, "")) || 0)}
              aria-label="Port"
            />
          </label>
          <label className="inline-flex items-center gap-1.5 text-text-muted min-h-8">
            <input type="checkbox" checked={form.secure} onChange={(e) => set("secure", e.target.checked)} /> TLS
          </label>
        </div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field id={id("db")} label="Database" hint="the one holding your tracker tables">
          <input
            id={id("db")}
            className={`${INPUT_CLS} font-mono`}
            value={form.database}
            onChange={(e) => set("database", e.target.value)}
          />
        </Field>
        <div>
          <label htmlFor={id("repos")} className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5">
            Repositories it holds <span className="text-[var(--color-text-subtle)] font-normal">(Org/repo)</span>
          </label>
          <RepoChips repos={form.repos} onChange={(r) => set("repos", r)} />
        </div>
        <Field id={id("user")} label="Read-only user" hint="SELECT on the views + system.columns">
          <input
            id={id("user")}
            className={`${INPUT_CLS} font-mono`}
            value={form.user}
            autoComplete="off"
            placeholder="loom_ro"
            onChange={(e) => set("user", e.target.value)}
          />
        </Field>
        <Field id={id("pw")} label="Password" hint="stored encrypted, never shown again">
          <input
            id={id("pw")}
            type="password"
            className={`${INPUT_CLS} font-mono`}
            value={form.password}
            autoComplete="new-password"
            onChange={(e) => set("password", e.target.value)}
          />
        </Field>
      </div>
      <div className="rounded-lg border border-border">
        <button
          type="button"
          aria-expanded={advanced}
          onClick={() => setAdvanced((v) => !v)}
          className="w-full flex items-center gap-1.5 px-3 min-h-10 text-sm text-left"
        >
          <ChevronRight
            size={13}
            aria-hidden="true"
            className={`transition-transform ${advanced ? "rotate-90" : ""}`}
          />
          Advanced
          <span className="text-xs text-text-muted truncate">
            — name <span className="font-mono">{form.name || "…"}</span>,{" "}
            {form.restricted
              ? "private, join by approval"
              : `public, ${form.join_policy === "open" ? "joins at once" : "join by approval"}`}
          </span>
          {nameProblem && !advanced && (
            <AlertTriangle size={12} className="text-warn ml-auto shrink-0" aria-label="needs attention" />
          )}
        </button>
        {advanced && (
          <div className="px-3 pb-3 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <Field id={id("name")} label="Name" hint="lower-case, 2–32">
                <input
                  id={id("name")}
                  className={`${INPUT_CLS} font-mono`}
                  value={form.name}
                  onChange={(e) => {
                    onTouch("name");
                    set("name", e.target.value.toLowerCase());
                  }}
                />
              </Field>
              <Field id={id("ns")} label="Namespace" hint="tracker-<team>">
                <input
                  id={id("ns")}
                  className={`${INPUT_CLS} font-mono`}
                  value={form.namespace}
                  onChange={(e) => {
                    onTouch("namespace");
                    set("namespace", e.target.value.toLowerCase());
                  }}
                />
              </Field>
            </div>
            <fieldset className="space-y-1.5 text-sm">
              <legend className="text-xs font-medium text-text-muted mb-1">Who can read it</legend>
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  name="tsrc-access"
                  className="mt-1"
                  checked={!form.restricted && form.join_policy === "open"}
                  onChange={() => {
                    set("restricted", false);
                    set("join_policy", "open");
                  }}
                />
                <span>Public — anyone in the org joins at once, read-only</span>
              </label>
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  name="tsrc-access"
                  className="mt-1"
                  checked={!form.restricted && form.join_policy === "approval"}
                  onChange={() => {
                    set("restricted", false);
                    set("join_policy", "approval");
                  }}
                />
                <span>Public, you approve each reader</span>
              </label>
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  name="tsrc-access"
                  className="mt-1"
                  checked={form.restricted}
                  onChange={() => set("restricted", true)}
                />
                <span className="inline-flex items-center gap-1">
                  <ShieldAlert size={13} aria-hidden="true" /> Private — approval, and a token must name it
                </span>
              </label>
            </fieldset>
          </div>
        )}
      </div>
      {problems.length > 0 && (form.host || form.user || form.password) && (
        <ul className="text-xs text-warn space-y-0.5" aria-label="Before the check">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---- 3. Check --------------------------------------------------------------

type CheckState = "pending" | "ok" | "fail" | "todo";

function CheckRow({ state, title, children }: { state: CheckState; title: string; children?: ReactNode }) {
  const icon =
    state === "ok" ? (
      <CheckCircle2 size={15} className="text-success" aria-hidden="true" />
    ) : state === "fail" ? (
      <XCircle size={15} className="text-danger" aria-hidden="true" />
    ) : state === "pending" ? (
      <Circle size={15} className="text-text-subtle animate-pulse motion-reduce:animate-none" aria-hidden="true" />
    ) : (
      <Circle size={15} className="text-text-subtle" aria-hidden="true" />
    );
  const word = { ok: "passed", fail: "failed", pending: "checking", todo: "not checked" }[state];
  return (
    <li className="flex items-start gap-2.5">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="text-sm">
          {title} <span className="sr-only">— {word}</span>
        </div>
        {children && <div className="mt-1 text-xs text-text-muted">{children}</div>}
      </div>
    </li>
  );
}

function CheckStep({
  form,
  busy,
  refusal,
  checked,
  stale,
}: {
  form: TrackerSourceForm;
  busy: boolean;
  refusal: { gate: Gate; message: string } | null;
  checked: TrackerSourceAddResult | null;
  stale: boolean;
}) {
  const order: Gate[] = ["reach", "readonly", "schema", "rows"];
  const failedAt = refusal ? order.indexOf(refusal.gate) : -1;
  const state = (g: Gate): CheckState => {
    if (busy) return "pending";
    if (refusal) {
      const i = order.indexOf(g);
      if (refusal.gate === g) return "fail";
      if (failedAt >= 0 && i < failedAt) return "ok";
      return "todo";
    }
    if (!checked) return "todo";
    if (g === "schema") return checked.contract.ok ? "ok" : "fail";
    if (g === "rows") return checked.contract.ok ? "ok" : "todo";
    return "ok";
  };
  return (
    <div className="space-y-4" aria-live="polite">
      {stale && (
        <div className="rounded-lg border border-warn/50 bg-warn/10 px-3 py-2 text-xs" role="status">
          The connection or the mapping changed since this check. Check again before adding.
        </div>
      )}
      <ul className={`space-y-3 ${stale ? "opacity-60" : ""}`} aria-label="Checks">
        <CheckRow state={state("reach")} title={`Reach ${form.host || "the host"} as ${form.user || "the user"}`}>
          {refusal?.gate === "reach" && <span className="text-danger">{refusal.message}</span>}
        </CheckRow>
        <CheckRow state={state("readonly")} title="The user can only read">
          {refusal?.gate === "readonly" && (
            <span className="text-danger">
              {refusal.message} — create a user with SELECT only: on the tables you map (or the views) and
              system.columns.
            </span>
          )}
          {checked && !refusal && checked.roles.length > 0 && (
            <>
              It also holds the roles <span className="font-mono">{checked.roles.join(", ")}</span> — make sure that's
              intended.
            </>
          )}
        </CheckRow>
        <CheckRow
          state={state("schema")}
          title={
            checked?.contract.mode === "mapping"
              ? `Your mapping fits the tables in ${form.database}`
              : `The views in ${form.database} match what Loom reads`
          }
        >
          {checked && !checked.contract.ok && !refusal && (
            <span>Fix the tables below, run the SQL, then Check again.</span>
          )}
        </CheckRow>
        <CheckRow
          state={state("rows")}
          title={
            checked?.contract.mode === "mapping"
              ? "Loom can read rows through your mapping"
              : "Loom can read rows through the views"
          }
        >
          {refusal?.gate === "rows" && <span className="text-danger">{refusal.message}</span>}
        </CheckRow>
      </ul>
      {refusal && (refusal.gate === "name" || refusal.gate === "other") && (
        <ErrorNote>
          {refusal.message}
          {refusal.gate === "name" && " — change the name under Advanced."}
        </ErrorNote>
      )}
      {checked && !refusal && (
        <>
          {checked.contract.mode !== "mapping" && (
            <ContractReportView report={checked.contract} templates={checked.view_templates} />
          )}
          {checked.contract.mode === "mapping" && (checked.contract.mapping_errors ?? []).length > 0 && (
            <ul className="text-xs text-danger space-y-0.5" aria-label="Mapping problems">
              {(checked.contract.mapping_errors ?? []).map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          )}
          {checked.contract.capabilities && <Capabilities caps={checked.contract.capabilities} />}
          {checked.contract.ok && !stale && (
            <p className="text-sm text-success flex items-center gap-1.5">
              <CheckCircle2 size={14} aria-hidden="true" /> Ready to add. Nothing has been saved yet.
            </p>
          )}
        </>
      )}
    </div>
  );
}

/** What the tracker's data lets loom answer, said before Add. */
function Capabilities({ caps }: { caps: Record<string, boolean> }) {
  const on = Object.entries(caps)
    .filter(([, v]) => v)
    .map(([k]) => k);
  const off = Object.entries(caps)
    .filter(([, v]) => !v)
    .map(([k]) => k);
  return (
    <section
      aria-label="What Loom can do with this tracker"
      className="rounded-lg border border-border p-3 space-y-1.5"
    >
      <h3 className="text-sm font-medium">What Loom can do with this tracker</h3>
      <ul className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
        {on.map((k) => (
          <li key={k} className="flex items-center gap-1.5">
            <CheckCircle2 size={12} className="text-success" aria-hidden="true" /> {CAPABILITY_LABEL[k] ?? k}
          </li>
        ))}
        {off.map((k) => (
          <li key={k} className="flex items-center gap-1.5 text-text-muted">
            <Circle size={12} aria-hidden="true" /> {CAPABILITY_LABEL[k] ?? k}{" "}
            <span className="sr-only">— not with this data</span>
          </li>
        ))}
      </ul>
      {off.length > 0 && (
        <p className="text-2xs text-text-subtle">
          Map more fields later (Trackers → Edit mapping) to switch the rest on.
        </p>
      )}
    </section>
  );
}

// ---- done ------------------------------------------------------------------

function DoneStep({ added, repo }: { added: TrackerSourceAddResult; repo?: string }) {
  const ns = added.namespaces[0];
  return (
    <div className="space-y-3 text-sm" role="status">
      <p className="flex items-center gap-2 text-base font-medium">
        <CheckCircle2 size={18} className="text-success" aria-hidden="true" /> {added.name} is connected
      </p>
      <ul className="space-y-1 text-text-muted list-disc pl-5">
        <li>
          Loom reads <span className="font-mono">{added.database}</span> on {added.host} live; nothing was copied.
        </li>
        {ns && (
          <li>
            Readers join <span className="font-mono">{ns.namespace}</span> (
            {ns.classification === "restricted" ? "private" : "public"},{" "}
            {ns.join_policy === "open" ? "joins at once" : "join by approval"}). You are its admin.
          </li>
        )}
        {repo && <li>{repo} is linked to it: agents in that repository's checkout see its issues and PRs.</li>}
        <li>Issue and PR counts appear on the card within a minute.</li>
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rotate / retire
// ---------------------------------------------------------------------------

export function RotateCredentialDialog({
  open,
  onOpenChange,
  org,
  name,
  onRotated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  name: string;
  onRotated: () => void;
}) {
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await rotateTrackerSource(org, name, user.trim(), password);
      onRotated();
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={460}
      icon={<KeyRound size={16} aria-hidden="true" />}
      title={`Rotate the credential · ${name}`}
      description="The new user is checked like the first one — read-only, and the views must still match — before it replaces the old one."
      footer={
        <>
          <CancelButton />
          <button
            type="button"
            className={PRIMARY_BTN}
            disabled={busy || !user.trim() || !password}
            onClick={() => void submit()}
          >
            {busy ? "Rotating…" : "Rotate"}
          </button>
        </>
      }
    >
      <Field id="rot-user" label="Read-only user">
        <input
          id="rot-user"
          className={`${INPUT_CLS} font-mono`}
          value={user}
          autoComplete="off"
          onChange={(e) => setUser(e.target.value)}
          autoFocus
        />
      </Field>
      <Field id="rot-pw" label="Password">
        <input
          id="rot-pw"
          type="password"
          className={`${INPUT_CLS} font-mono`}
          value={password}
          autoComplete="new-password"
          onChange={(e) => setPassword(e.target.value)}
        />
      </Field>
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}

export function RetireSourceDialog({
  open,
  onOpenChange,
  org,
  name,
  namespaces,
  onRetired,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  name: string;
  namespaces: string[];
  onRetired: (retired: string[]) => void;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await removeTrackerSource(org, name, typed.trim());
      onRetired(out.namespaces_retired);
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={460}
      icon={<Trash2 size={16} aria-hidden="true" />}
      title={`Retire ${name}?`}
      description={
        <>
          Loom stops reading the database and retires {namespaces.length === 1 ? "its namespace" : "its namespaces"}{" "}
          <span className="font-mono">{namespaces.join(", ")}</span>: every reader loses access and every repository
          linked to it answers nothing. Your data in ClickHouse is not touched.
        </>
      }
      footer={
        <>
          <CancelButton />
          <button
            type="button"
            className={DANGER_BTN}
            disabled={busy || typed.trim() !== name}
            onClick={() => void submit()}
          >
            {busy ? "Retiring…" : "Retire tracker"}
          </button>
        </>
      }
    >
      <Field
        id="retire-name"
        label={
          <>
            Type <span className="font-mono">{name}</span> to confirm
          </>
        }
      >
        <input
          id="retire-name"
          className={`${INPUT_CLS} font-mono`}
          value={typed}
          autoComplete="off"
          onChange={(e) => setTyped(e.target.value)}
          autoFocus
        />
      </Field>
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}

// ---------------------------------------------------------------------------
// Edit a tracker's mapping
// ---------------------------------------------------------------------------

export function EditMappingDialog({
  open,
  onOpenChange,
  org,
  source,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  source: {
    name: string;
    database: string;
    mode?: "mapping" | "views";
    mapping?: SourceMapping | null;
    repos: string[];
  };
  onSaved: () => void;
}) {
  const [inspected, setInspected] = useState<TrackerInspectResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mapping, setMapping] = useState<SourceMapping>(source.mapping ?? { version: 1, tables: {} });
  const [busy, setBusy] = useState<"check" | "save" | null>(null);
  const [checked, setChecked] = useState<{ fp: string; contract: TrackerContractReport } | null>(null);
  useEffect(() => {
    let live = true;
    inspectTrackerSource({ org, name: source.name, repos: source.repos })
      .then((out) => {
        if (!live) return;
        setInspected(out);
        if (!source.mapping && out.proposal.mapping) setMapping(out.proposal.mapping);
      })
      .catch((e) => {
        if (live) setError(humanizeError(e));
      });
    return () => {
      live = false;
    };
  }, [org, source.name, source.mapping, source.repos]);
  const fp = JSON.stringify(mapping);
  const problems = mappingProblems(mapping);
  const run = async (dryRun: boolean) => {
    setBusy(dryRun ? "check" : "save");
    setError(null);
    try {
      const out = await setTrackerMapping(org, source.name, mapping, dryRun);
      if (dryRun) setChecked({ fp, contract: out.contract });
      else {
        onSaved();
        onOpenChange(false);
      }
    } catch (e) {
      setError(humanizeError(e));
      if (dryRun) setChecked(null);
    } finally {
      setBusy(null);
    }
  };
  const passed = checked !== null && checked.fp === fp && checked.contract.ok === true;
  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={760}
      icon={<Database size={16} aria-hidden="true" />}
      title={`Edit mapping · ${source.name}`}
      description="Which of your tables and columns Loom reads for each field. Check reads a row through the new mapping before anything is saved."
      footer={
        <>
          <CancelButton />
          <button
            type="button"
            className={SECONDARY_BTN}
            disabled={busy !== null || problems.length > 0 || !inspected}
            onClick={() => void run(true)}
          >
            {busy === "check" ? "Checking…" : "Check"}
          </button>
          <button
            type="button"
            className={PRIMARY_BTN}
            disabled={busy !== null || !passed}
            onClick={() => void run(false)}
          >
            {busy === "save" ? "Saving…" : "Save mapping"}
          </button>
        </>
      }
    >
      {!inspected && !error && (
        <div className="space-y-2" role="status" aria-label="Reading the tracker's tables">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
      {inspected && (
        <MappingEditor
          database={source.database}
          tables={inspected.tables}
          mapping={mapping}
          proposal={inspected.proposal}
          onChange={setMapping}
        />
      )}
      {problems.length > 0 && inspected && (
        <ul className="text-xs text-warn space-y-0.5" aria-label="Before the check">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {checked && (
        <div className={checked.fp !== fp ? "opacity-60" : ""}>
          {checked.contract.ok ? (
            <p className="text-sm text-success flex items-center gap-1.5">
              <CheckCircle2 size={14} aria-hidden="true" /> The mapping reads. Nothing has been saved yet.
            </p>
          ) : (
            <ErrorNote>{checked.contract.summary}</ErrorNote>
          )}
          {checked.contract.capabilities && <Capabilities caps={checked.contract.capabilities} />}
        </div>
      )}
    </DialogFrame>
  );
}
