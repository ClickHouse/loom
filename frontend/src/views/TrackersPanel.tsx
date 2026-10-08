/**
 * The org's trackers in one place on the Repositories page — every tracker with its state
 * in plain words, the repositories it lists, its scopes and counts, and, for its admin,
 * Check / Rotate / Retire. Adding one starts here too, for anyone signed in (not only a
 * repository's maintainer), when the deployment serves self-service. Reads nothing of its
 * own: the page's one `tracker.source.list`.
 */
import { lazy, Suspense, useState } from "react";
import { Database, Plus } from "lucide-react";
import { checkTrackerSource } from "../api";
import { fmtInt, humanizeError, relativeTime } from "../lib/format";
import {
  canAdminister,
  freshnessOf,
  operatorOwned,
  trackerAnchor,
  trackerStatus,
  type TrackerSource,
  type TrackerSourceCheckResult,
  type TrackerTone,
} from "../lib/trackerSources";
import { toast } from "../components/Toast";
import { BTN, BTN_TONE } from "./RepositoryCard";

const ContractReportView = lazy(() =>
  import("../flows/TrackerSourceDialogs").then((m) => ({ default: m.ContractReportView })),
);
const RotateCredentialDialog = lazy(() =>
  import("../flows/TrackerSourceDialogs").then((m) => ({ default: m.RotateCredentialDialog })),
);
const RetireSourceDialog = lazy(() =>
  import("../flows/TrackerSourceDialogs").then((m) => ({ default: m.RetireSourceDialog })),
);
const EditMappingDialog = lazy(() =>
  import("../flows/TrackerSourceDialogs").then((m) => ({ default: m.EditMappingDialog })),
);

const TONE_PILL: Record<TrackerTone, string> = {
  ok: "border-success/40 text-success",
  warn: "border-warn/50 text-warn",
  danger: "border-danger/50 text-danger",
  muted: "border-border text-text-muted",
};

export function TrackersPanel({
  org,
  sources,
  selfService,
  loading,
  onAdd,
  onChanged,
}: {
  org: string;
  sources: TrackerSource[];
  selfService: boolean;
  loading: boolean;
  onAdd: () => void;
  onChanged: () => void;
}) {
  return (
    <section aria-labelledby="trackers-heading" className="mt-8">
      <div className="flex flex-wrap items-end justify-between gap-3 mb-3">
        <div className="min-w-0">
          <h2 id="trackers-heading" className="text-lg font-semibold tracking-tight inline-flex items-center gap-2">
            <Database size={16} aria-hidden="true" /> Trackers
          </h2>
          <p className="text-xs text-text-muted mt-0.5 max-w-2xl">
            ClickHouse databases of issues, PRs and incidents that Loom reads live. A repository links to one, and
            agents in its checkout see its issues.
          </p>
        </div>
      </div>
      {loading && (
        <p className="text-xs text-text-muted" role="status">
          Reading the trackers…
        </p>
      )}
      {!loading && sources.length === 0 && (
        <div className="rounded-lg border border-dashed border-border p-4 text-sm text-text-muted flex flex-wrap items-center gap-3">
          <span className="min-w-0 flex-1">
            No tracker is connected yet.
            {selfService
              ? " Add one: create a few views in your ClickHouse, give Loom a read-only user, and link the repositories it covers."
              : " Ask the operator to connect one."}
          </span>
          {selfService && org && (
            <button type="button" onClick={onAdd} className={`${BTN} ${BTN_TONE.primary}`}>
              <Plus size={13} aria-hidden="true" /> Add a tracker
            </button>
          )}
        </div>
      )}
      {sources.length > 0 && (
        <ul className="space-y-2" aria-label="Trackers">
          {sources.map((s) => (
            <TrackerRow key={s.name} org={org} s={s} admin={canAdminister(s, selfService)} onChanged={onChanged} />
          ))}
        </ul>
      )}
    </section>
  );
}

function TrackerRow({
  org,
  s,
  admin,
  onChanged,
}: {
  org: string;
  s: TrackerSource;
  admin: boolean;
  onChanged: () => void;
}) {
  const status = trackerStatus(s);
  const f = freshnessOf(s);
  const repos = [...new Set(s.namespaces.flatMap((n) => n.repos))];
  const [check, setCheck] = useState<{ busy: boolean; result?: TrackerSourceCheckResult; error?: string }>({
    busy: false,
  });
  const [dialog, setDialog] = useState<"rotate" | "retire" | "mapping" | null>(null);
  const runCheck = async () => {
    setCheck({ busy: true });
    try {
      setCheck({ busy: false, result: await checkTrackerSource(org, s.name) });
    } catch (e) {
      setCheck({ busy: false, error: humanizeError(e) });
    }
  };
  return (
    <li id={trackerAnchor(s.name)} className="rounded-lg border border-border bg-surface p-3 scroll-mt-20">
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{s.name}</span>
            <span
              className={`inline-flex items-center rounded-full border px-2 text-2xs min-h-5 ${TONE_PILL[status.tone]}`}
              title={status.detail}
            >
              {status.label}
            </span>
            <span className="text-2xs text-text-subtle">
              {operatorOwned(s) ? "set up by the operator" : "added from the dashboard"}
            </span>
            {/* Read through a mapping of the team's own tables, or loom's views */}
            <span className="text-2xs text-text-subtle">
              · {s.mode === "mapping" ? "your tables, mapped" : "Loom's views"}
            </span>
          </div>
          <p className="text-xs text-text-muted mt-0.5 break-words">
            <span className="font-mono">{s.database || "—"}</span>
            {s.host ? (
              <>
                {" "}
                on <span className="font-mono">{s.host}</span>
              </>
            ) : null}
            {f && (
              <>
                {" "}
                · {fmtInt(f.issues)} issues · {fmtInt(f.prs)} PRs
                {f.last_refresh_at ? ` · refreshed ${relativeTime(f.last_refresh_at)}` : ""}
              </>
            )}
          </p>
          {status.tone !== "ok" && (
            <p className={`text-xs mt-1 ${status.tone === "danger" ? "text-danger" : "text-text-muted"}`}>
              {status.detail}
            </p>
          )}
          <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted">
            <span>
              Lists:{" "}
              {repos.length
                ? repos.map((r) => (
                    <span key={r} className="font-mono text-text mr-2">
                      {r}
                    </span>
                  ))
                : "no repository"}
            </span>
            <span>
              Scopes:{" "}
              {s.namespaces.map((n) => (
                <span key={n.namespace} className="mr-2">
                  <span className="text-text">{n.classification === "restricted" ? "private" : "public"}</span>{" "}
                  <span className="font-mono">{n.namespace}</span>
                </span>
              ))}
            </span>
          </div>
        </div>
        {admin && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <button type="button" className={ADMIN_LINK} disabled={check.busy} onClick={() => void runCheck()}>
              {check.busy ? "Checking…" : "Check now"}
            </button>
            <button type="button" className={ADMIN_LINK} onClick={() => setDialog("mapping")}>
              Edit mapping
            </button>
            <button type="button" className={ADMIN_LINK} onClick={() => setDialog("rotate")}>
              Rotate credential
            </button>
            <button type="button" className={`${ADMIN_LINK} text-danger`} onClick={() => setDialog("retire")}>
              Retire
            </button>
          </div>
        )}
      </div>
      {check.error && (
        <p className="mt-2 text-xs text-danger" role="alert">
          {check.error}
        </p>
      )}
      {check.result && (
        <div className="mt-3 border-t border-border pt-3" role="status">
          {!check.result.reachable ? (
            <p className="text-xs text-danger">Couldn't reach it: {check.result.contract.summary}</p>
          ) : (
            <Suspense fallback={null}>
              <ContractReportView report={check.result.contract} templates={check.result.view_templates} />
            </Suspense>
          )}
        </div>
      )}
      {dialog === "rotate" && (
        <Suspense fallback={null}>
          <RotateCredentialDialog
            open
            onOpenChange={(v) => {
              if (!v) setDialog(null);
            }}
            org={org}
            name={s.name}
            onRotated={() => {
              toast(`${s.name}: credential rotated; reads reconnect with it`, "ok");
              onChanged();
            }}
          />
        </Suspense>
      )}
      {dialog === "mapping" && (
        <Suspense fallback={null}>
          <EditMappingDialog
            open
            onOpenChange={(v) => {
              if (!v) setDialog(null);
            }}
            org={org}
            source={{ name: s.name, database: s.database, mode: s.mode, mapping: s.mapping, repos }}
            onSaved={() => {
              toast(`${s.name}: mapping saved; reads follow within a minute`, "ok");
              onChanged();
            }}
          />
        </Suspense>
      )}
      {dialog === "retire" && (
        <Suspense fallback={null}>
          <RetireSourceDialog
            open
            onOpenChange={(v) => {
              if (!v) setDialog(null);
            }}
            org={org}
            name={s.name}
            namespaces={s.namespaces.map((n) => n.namespace)}
            onRetired={(ns) => {
              toast(`${s.name} retired${ns.length ? ` · ${ns.join(", ")}` : ""}`, "ok");
              onChanged();
            }}
          />
        </Suspense>
      )}
    </li>
  );
}

const ADMIN_LINK =
  "min-h-8 px-1 -mx-1 rounded text-text-muted hover:text-text underline-offset-2 hover:underline disabled:opacity-50 pointer-coarse:min-h-11";
