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
import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { Alert } from "../components/ui/Alert";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { Table } from "@clickhouse/click-ui/Table";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  addTrackerSource,
  getTrackerContract,
  inspectTrackerSource,
  removeTrackerSource,
  rotateTrackerSource,
  setTrackerMapping,
} from "../api";
import { ApiError } from "../lib/errors";
import { humanizeError } from "../lib/errorWords";
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
import { IconButton } from "../components/IconButton";
import { Skeleton } from "../components/Skeleton";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { LoadingSkeleton } from "../components/ui/Loading";
import { Unavailable } from "../components/ui/Unavailable";
import { MappingEditor } from "./TrackerMapping";
import { CancelButton, DialogFrame, ErrorNote } from "./TaskDialogFrame";

/** A field label with its hint in parentheses. */
function hinted(label: string, hint: string): ReactNode {
  return (
    <>
      {label} <span className="font-normal text-text-muted">({hint})</span>
    </>
  );
}

const SUCCESS = "var(--color-success)";
const WARN = "var(--color-warn)";
const DANGER = "var(--color-danger)";
const SUBTLE = "var(--color-text-muted)";
const MUTED = "var(--color-text-muted)";

// ---------------------------------------------------------------------------
// The check report, table by table (shared with a tracker's "Check now")
// ---------------------------------------------------------------------------

const STATE_ICON: Record<ContractTableRow["state"], { icon: IconName; color: string; word: string }> = {
  ok: { icon: "check-in-circle", color: SUCCESS, word: "ok" },
  aliased: { icon: "check-in-circle", color: SUCCESS, word: "ok" },
  incomplete: { icon: "warning", color: WARN, word: "incomplete" },
  missing: { icon: "cross", color: DANGER, word: "missing" },
  absent: { icon: "info-in-circle", color: MUTED, word: "absent" },
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
          return (
            <li key={r.table} className="flex items-start gap-2 text-xs min-w-0">
              <Icon name={s.icon} size="xs" color={s.color} aria-hidden="true" className="shrink-0 mt-0.5" />
              <span className="font-mono text-text shrink-0">{r.table}</span>
              <span className="sr-only">{s.word}</span>
              <span className="text-text-muted min-w-0 break-words">
                {!r.required && <span className="text-text-muted">optional · </span>}
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
            {drafts.length > 1 && <CopyButton text={all} label="Copy all fixes" />}
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
        <CopyButton text={sql} label="Copy SQL" ariaLabel={copyLabel} />
      </div>
      <pre className="max-h-56 overflow-auto p-2 text-xs leading-relaxed font-mono whitespace-pre">{sql}</pre>
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
          <Unavailable reason={problems[0]}>
            <Button
              type="primary"
              htmlType="button"
              iconRight="chevron-right"
              loading={busy === "inspect"}
              disabled={busy !== null}
              onClick={() => void inspect()}
            >
              Next: map your tables
            </Button>
          </Unavailable>
        </>
      );
    if (step === "map")
      return (
        <>
          <Button type="secondary" htmlType="button" onClick={() => setStep("connect")}>
            Back
          </Button>
          <Unavailable reason={mapProblems[0]}>
            <Button
              type="primary"
              htmlType="button"
              iconRight="chevron-right"
              disabled={busy !== null}
              onClick={goCheck}
            >
              Check
            </Button>
          </Unavailable>
        </>
      );
    if (step === "check")
      return (
        <>
          <Button
            type="secondary"
            htmlType="button"
            disabled={busy !== null}
            onClick={() => setStep(inspected ? "map" : "connect")}
          >
            {inspected ? "Edit mapping" : "Edit connection"}
          </Button>
          <Button
            type="secondary"
            htmlType="button"
            loading={busy === "check"}
            disabled={busy !== null || problems.length > 0}
            onClick={() => void run(true)}
          >
            Check again
          </Button>
          <Button
            type="primary"
            htmlType="button"
            loading={busy === "add"}
            disabled={busy !== null || !passed}
            onClick={() => void run(false)}
          >
            {repo ? `Add and link ${repo}` : "Add tracker"}
          </Button>
        </>
      );
    return (
      <Button type="primary" htmlType="button" onClick={() => onOpenChange(false)}>
        Done
      </Button>
    );
  })();

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={760}
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
  const viewsHintId = useId();
  const db = form.database.trim();
  if (inspected.proposal.mode === "views") {
    return (
      <Alert
        role="status"
        state="success"
        text={
          <>
            <strong className="font-medium">
              <span className="font-mono">{db}</span> already exposes Loom's tables — nothing to map.
            </strong>{" "}
            Loom reads <span className="font-mono">github_issues</span>, <span className="font-mono">github_prs</span>{" "}
            and <span className="font-mono">edges</span> there as they are. Go to Check.
          </>
        }
      />
    );
  }
  return (
    <div className="space-y-4">
      <Alert
        state="info"
        text={
          <>
            <strong className="font-medium">Your schema, not Loom's:</strong> pick the table that holds your items and
            which of its columns holds each field. Loom proposed what it could match — check the ones marked "guessed".
            Fields you leave on "Loom default" are filled in for you. Loom reads {inspected.tables.length} table
            {inspected.tables.length === 1 ? "" : "s"} in <span className="font-mono">{db}</span>; its read-only user
            needs SELECT on the tables you map.
          </>
        }
      />
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
          <Icon
            name="chevron-right"
            size="xs"
            aria-hidden="true"
            className="transition-transform group-open:rotate-90"
          />
          Prefer views?{" "}
          <span className="text-xs text-text-muted">— create Loom's tables in your database yourself</span>
        </summary>
        <div className="px-3 pb-3 space-y-3">
          <div>
            <Checkbox
              checked={mode === "views"}
              onCheckedChange={(x) => onMode(x === true ? "views" : "mapping")}
              aria-describedby={viewsHintId}
              label="I created Loom's views — read them instead of a mapping"
            />
            <p id={viewsHintId} className="mt-1 ml-6 text-xs text-text-muted">
              Loom reads them in <span className="font-mono">{db}</span>.
            </p>
          </div>
          {views && <ViewsStep org={org} database={db} />}
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
              className={`inline-flex items-center gap-1.5 rounded-full px-2.5 min-h-8 border ${state === "current" ? "border-accent bg-accent-soft text-text font-medium" : state === "done" ? "border-border text-text" : "border-border text-text-muted"} disabled:cursor-default`}
            >
              {state === "done" ? (
                <Icon name="check-in-circle" size="xs" color={SUCCESS} aria-hidden="true" />
              ) : (
                <span className="tabular-nums" aria-hidden="true">
                  {i + 1}
                </span>
              )}
              <span aria-hidden="true">{s.label}</span>
              <span className="sr-only">{`Step ${i + 1} of ${STEPS.length}: ${s.label}${state === "done" ? " (done)" : ""}`}</span>
            </button>
            {i < STEPS.length - 1 && (
              <Icon name="chevron-right" size="xs" color={SUBTLE} aria-hidden="true" className="shrink-0" />
            )}
          </li>
        );
      })}
    </ol>
  );
}

// ---- 1. Views --------------------------------------------------------------

function ViewsStep({ org, database }: { org: string; database: string }) {
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
      <Alert
        state="info"
        text={
          <>
            <strong className="font-medium">Who decides the schema:</strong> Loom reads fixed view names and columns;
            you decide how your tables map onto them, as views in your own ClickHouse. Loom only ever reads. Create the
            three required views below, then connect. Already have them? Go straight to Next.
          </>
        }
      />
      <TextField
        label={hinted("Database for the views", "create it if it does not exist")}
        value={db}
        placeholder="loom_tracker"
        onChange={(v) => setDb(v)}
      />
      {error && (
        <ErrorNote>
          Couldn't load the view list — {error}. You can still connect; the check lists what is missing.
        </ErrorNote>
      )}
      {!cat && !error && (
        <LoadingSkeleton label="Loading the views">
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        </LoadingSkeleton>
      )}
      {cat && (
        <>
          <section aria-label="Required views" className="space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3>Required — Loom does not read the tracker without these</h3>
              <div className="flex flex-wrap gap-2">
                <CopyButton
                  text={() =>
                    templatesFile(
                      cat,
                      required.map((t) => t.table),
                    )
                  }
                  label="Copy SQL"
                  ariaLabel="Copy the SQL for the required views"
                />
                <Button
                  type="secondary"
                  htmlType="button"
                  iconLeft="download"
                  onClick={() =>
                    download(
                      required.map((t) => t.table),
                      "loom-tracker-views.sql",
                    )
                  }
                >
                  Download .sql
                </Button>
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
            <h3>A read-only user for Loom</h3>
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
              <Icon
                name="chevron-right"
                size="xs"
                aria-hidden="true"
                className="transition-transform group-open:rotate-90"
              />
              Optional views ({optional.length}){" "}
              <span className="text-xs text-text-muted">
                — incidents, checks, releases; add them when you have the data
              </span>
            </summary>
            <div className="px-3 pb-3 space-y-2">
              <Button
                type="secondary"
                htmlType="button"
                iconLeft="download"
                onClick={() =>
                  download(
                    cat.tables.map((t) => t.table),
                    "loom-tracker-views-all.sql",
                  )
                }
              >
                {`Download all ${cat.tables.length} views`}
              </Button>
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
        <Icon
          name="chevron-right"
          size="xs"
          aria-hidden="true"
          className="transition-transform group-open:rotate-90 shrink-0"
        />
        <span className="font-mono text-sm">{t.table}</span>
        <span className="text-xs text-text-muted">{t.note}</span>
        <span className="text-xs text-text-muted ml-auto">
          {t.columns.length} column{t.columns.length === 1 ? "" : "s"}
          {must ? ` · ${must} must map` : ""}
        </span>
      </summary>
      <div className="px-3 pb-3">
        <Table
          size="sm"
          aria-label={`Columns of ${t.table}`}
          headers={[{ label: "Column" }, { label: "Type" }, { label: "Holds" }]}
          rows={t.columns.map((c) => ({
            id: c.name,
            items: [
              {
                label: (
                  <span className="inline-flex items-center gap-1.5">
                    <span className="font-mono">{c.name}</span>
                    {c.must_map && <Badge size="sm" state="warning" text="must map" />}
                  </span>
                ),
              },
              { label: <span className="font-mono">{c.type}</span> },
              { label: c.note || "—" },
            ],
          }))}
        />
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
    <div className="chip-field flex flex-wrap items-center min-h-8">
      {repos.map((r) => (
        <span key={r} className="inline-flex items-center gap-1 rounded-full bg-surface-3 pl-2 text-xs font-mono">
          {r}
          <IconButton
            icon="cross"
            size="xs"
            label={`Remove ${r}`}
            onClick={() => onChange(repos.filter((x) => x !== r))}
          />
        </span>
      ))}
      <input
        id="tsrc-repos"
        aria-label="Repositories it holds"
        className="flex-1 min-w-32 bg-transparent font-mono text-sm"
        value={draft}
        placeholder={repos.length ? "Add another…" : "Org/repo"}
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
          <Icon
            name="chevron-right"
            size="xs"
            aria-hidden="true"
            className="transition-transform group-open:rotate-90"
          />
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
      <TextField
        label={hinted("ClickHouse address", "paste the URL from your console, or a host")}
        value={address}
        autoFocus
        placeholder="https://abc.us-east-1.aws.clickhouse.cloud:8443"
        onChange={onAddress}
      />
      {form.host && (
        <div className="flex flex-wrap items-end gap-3 text-xs">
          <span className="text-text-muted">
            Host <span className="font-mono text-text">{form.host}</span>
          </span>
          <TextField
            label="Port"
            orientation="horizontal"
            className="w-36"
            inputMode="numeric"
            value={String(form.port)}
            onChange={(v) => set("port", Number(v.replace(/\D/g, "")) || 0)}
          />
          <Checkbox label="TLS" checked={form.secure} onCheckedChange={(x) => set("secure", x === true)} />
        </div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <TextField
          label={hinted("Database", "the one holding your tracker tables")}
          value={form.database}
          onChange={(v) => set("database", v)}
        />
        <div>
          <label htmlFor={id("repos")} className="block text-xs font-medium text-text-muted mb-1.5">
            {hinted("Repositories it holds", "Org/repo")}
          </label>
          <RepoChips repos={form.repos} onChange={(r) => set("repos", r)} />
        </div>
        <TextField
          label={hinted("Read-only user", "SELECT on the views + system.columns")}
          value={form.user}
          autoComplete="off"
          placeholder="loom_ro"
          onChange={(v) => set("user", v)}
        />
        <PasswordField
          label={hinted("Password", "stored encrypted, never shown again")}
          value={form.password}
          autoComplete="new-password"
          onChange={(v) => set("password", v)}
        />
      </div>
      <div className="rounded-lg border border-border">
        <button
          type="button"
          aria-expanded={advanced}
          onClick={() => setAdvanced((v) => !v)}
          className="w-full flex items-center gap-1.5 px-3 min-h-10 text-sm text-left"
        >
          <Icon
            name="chevron-right"
            size="xs"
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
            <Icon name="warning" size="xs" color={WARN} aria-label="needs attention" className="ml-auto shrink-0" />
          )}
        </button>
        {advanced && (
          <div className="px-3 pb-3 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <TextField
                label={hinted("Name", "lower-case, 2–32")}
                value={form.name}
                onChange={(v) => {
                  onTouch("name");
                  set("name", v.toLowerCase());
                }}
              />
              <TextField
                label={hinted("Namespace", "tracker-<team>")}
                value={form.namespace}
                onChange={(v) => {
                  onTouch("namespace");
                  set("namespace", v.toLowerCase());
                }}
              />
            </div>
            <ChoiceGroup
              label="Who can read it"
              value={form.restricted ? "private" : form.join_policy}
              onChange={(v) => {
                if (v === "private") {
                  set("restricted", true);
                  return;
                }
                set("restricted", false);
                set("join_policy", v);
              }}
              options={[
                { value: "open", label: "Public — anyone in the org joins at once, read-only" },
                { value: "approval", label: "Public, you approve each reader" },
                { value: "private", label: "Private — approval, and a token must name it" },
              ]}
            />
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

function CheckRow({
  state,
  title,
  stale = false,
  children,
}: {
  state: CheckState;
  title: string;
  /** The result predates an edit: shown in the muted text colour, not in its pass/fail colour. */
  stale?: boolean;
  children?: ReactNode;
}) {
  const icon =
    state === "ok" ? (
      <Icon name="check-in-circle" size="sm" color={stale ? MUTED : SUCCESS} aria-hidden="true" />
    ) : state === "fail" ? (
      <Icon name="cross" size="sm" color={stale ? MUTED : DANGER} aria-hidden="true" />
    ) : (
      <Icon
        name="circle"
        size="sm"
        color={SUBTLE}
        aria-hidden="true"
        className={state === "pending" ? "animate-pulse motion-reduce:animate-none" : undefined}
      />
    );
  const word = { ok: "passed", fail: "failed", pending: "checking", todo: "not checked" }[state];
  return (
    <li className="flex items-start gap-2.5">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className={`text-sm ${stale ? "text-text-muted" : ""}`}>
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
        <Alert
          role="status"
          state="warning"
          text="The connection or the mapping changed since this check. Check again before adding."
        />
      )}
      <ul className="space-y-3" aria-label="Checks">
        <CheckRow
          state={state("reach")}
          stale={stale}
          title={`Reach ${form.host || "the host"} as ${form.user || "the user"}`}
        >
          {refusal?.gate === "reach" && <span className="text-danger">{refusal.message}</span>}
        </CheckRow>
        <CheckRow state={state("readonly")} stale={stale} title="The user can only read">
          {refusal?.gate === "readonly" && <span className="text-danger">{refusal.message}</span>}
          {checked && !refusal && checked.roles.length > 0 && (
            <>
              It also holds the roles <span className="font-mono">{checked.roles.join(", ")}</span>, which grant read
              access only.
            </>
          )}
        </CheckRow>
        <CheckRow
          state={state("schema")}
          stale={stale}
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
          stale={stale}
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
            <div className="text-sm text-success flex items-center gap-1.5">
              <Icon name="check-in-circle" size="sm" color={SUCCESS} aria-hidden="true" /> Ready to add. Nothing has
              been saved yet.
            </div>
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
      <h3>What Loom can do with this tracker</h3>
      <ul className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
        {on.map((k) => (
          <li key={k} className="flex items-center gap-1.5">
            <Icon name="check-in-circle" size="xs" color={SUCCESS} aria-hidden="true" /> {CAPABILITY_LABEL[k] ?? k}
          </li>
        ))}
        {off.map((k) => (
          <li key={k} className="flex items-center gap-1.5 text-text-muted">
            <Icon name="circle" size="xs" color={MUTED} aria-hidden="true" /> {CAPABILITY_LABEL[k] ?? k}{" "}
            <span className="sr-only">— not with this data</span>
          </li>
        ))}
      </ul>
      {off.length > 0 && (
        <p className="text-xs text-text-muted">
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
      <div className="flex items-center gap-2 text-sm font-medium">
        <Icon name="check-in-circle" size="md" color={SUCCESS} aria-hidden="true" /> {added.name} is connected
      </div>
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
      title={`Rotate the credential · ${name}`}
      description="The new user is checked like the first one — read-only, and the views must still match — before it replaces the old one."
      footer={
        <>
          <CancelButton />
          <Button
            type="primary"
            htmlType="button"
            loading={busy}
            disabled={!user.trim() || !password}
            onClick={() => void submit()}
          >
            Rotate
          </Button>
        </>
      }
    >
      <TextField label="Read-only user" value={user} autoComplete="off" onChange={setUser} autoFocus />
      <PasswordField label="Password" value={password} autoComplete="new-password" onChange={setPassword} />
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
      title={`Retire ${name}?`}
      description={`Loom stops reading the database and retires ${namespaces.length === 1 ? "its namespace" : "its namespaces"} ${namespaces.join(", ")}: every reader loses access and every repository linked to it answers nothing. Your data in ClickHouse is not touched.`}
      footer={
        <>
          <CancelButton />
          <Button
            type="danger"
            htmlType="button"
            loading={busy}
            disabled={typed.trim() !== name}
            onClick={() => void submit()}
          >
            Retire tracker
          </Button>
        </>
      }
    >
      <TextField
        label={
          <>
            Type <span className="font-mono">{name}</span> to confirm
          </>
        }
        value={typed}
        autoComplete="off"
        onChange={setTyped}
        autoFocus
      />
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
      title={`Edit mapping · ${source.name}`}
      description="Which of your tables and columns Loom reads for each field. Check reads a row through the new mapping before anything is saved."
      footer={
        <>
          <CancelButton />
          <Button
            type="secondary"
            htmlType="button"
            loading={busy === "check"}
            disabled={busy !== null || problems.length > 0 || !inspected}
            onClick={() => void run(true)}
          >
            Check
          </Button>
          <Button
            type="primary"
            htmlType="button"
            loading={busy === "save"}
            disabled={busy !== null || !passed}
            onClick={() => void run(false)}
          >
            Save mapping
          </Button>
        </>
      }
    >
      {!inspected && !error && (
        <LoadingSkeleton label="Loading the tracker's tables">
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        </LoadingSkeleton>
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
        <div>
          {checked.fp !== fp && (
            <p className="text-sm text-text-muted mb-1">This result is from before your last change. Check again.</p>
          )}
          {checked.contract.ok ? (
            <div
              className={`text-sm flex items-center gap-1.5 ${checked.fp !== fp ? "text-text-muted" : "text-success"}`}
            >
              <Icon name="check-in-circle" size="sm" color={checked.fp !== fp ? MUTED : SUCCESS} aria-hidden="true" />{" "}
              The mapping reads. Nothing has been saved yet.
            </div>
          ) : (
            <ErrorNote>{checked.contract.summary}</ErrorNote>
          )}
          {checked.contract.capabilities && <Capabilities caps={checked.contract.capabilities} />}
        </div>
      )}
    </DialogFrame>
  );
}
