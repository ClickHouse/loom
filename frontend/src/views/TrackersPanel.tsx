/**
 * The org's trackers in one place on the Repositories page — every tracker with its state
 * in plain words, the repositories it lists, its scopes and counts, and, for its admin,
 * Check / Rotate / Retire. Adding one starts here too, for anyone signed in (not only a
 * repository's maintainer), when the deployment serves self-service. Reads nothing of its
 * own: the page's one `tracker.source.list`.
 */
import { lazy, Suspense, useState } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { checkTrackerSource } from "../api";
import { fmtInt, relativeTime } from "../lib/format";
import { humanizeError } from "../lib/errorWords";
import {
  canAdminister,
  freshnessOf,
  operatorOwned,
  trackerAnchor,
  trackerStatus,
  trackerVerbs,
  type TrackerSource,
  type TrackerSourceCheckResult,
  type TrackerTone,
} from "../lib/trackerSources";
import { useStore } from "../lib/state";
import { toast } from "../components/Toast";
import { Loading, SectionHeading, TipTarget } from "../components/ui";

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

const TONE_STATE: Record<TrackerTone, BadgeState> = {
  ok: "success",
  warn: "warning",
  danger: "danger",
  muted: "neutral",
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
          <SectionHeading level={2} id="trackers-heading" label="Trackers" />
          <p className="text-xs text-text-muted max-w-2xl">
            ClickHouse databases of issues, PRs and incidents that Loom reads live. A repository links to one, and
            agents in its checkout see its issues.
          </p>
        </div>
      </div>
      {loading && <Loading layout="inline" label="Loading the trackers" />}
      {!loading && sources.length === 0 && (
        <div className="rounded-lg border border-dashed border-border p-4 text-sm text-text-muted flex flex-wrap items-center gap-3">
          <span className="min-w-0 flex-1">
            No tracker is connected yet.
            {selfService
              ? " Add one: create a few views in your ClickHouse, give Loom a read-only user, and link the repositories it covers."
              : " Ask the operator to connect one."}
          </span>
          {selfService && org && (
            <Button type="secondary" htmlType="button" iconLeft="plus" onClick={onAdd}>
              Add a tracker
            </Button>
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
  const whoami = useStore((st) => st.whoami);
  const verbs = trackerVerbs(s, org, whoami);
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
            <TipTarget tip={status.detail}>
              <Badge text={status.label} state={TONE_STATE[status.tone]} size="sm" />
            </TipTarget>
            <span className="text-xs text-text-muted">
              {operatorOwned(s) ? "set up by the operator" : "added from the dashboard"}
            </span>
            {/* Read through a mapping of the team's own tables, or loom's views */}
            <span className="text-xs text-text-muted">
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
          <div className="flex flex-wrap items-center gap-2">
            <Button type="empty" htmlType="button" loading={check.busy} onClick={() => void runCheck()}>
              Check now
            </Button>
            <Button type="empty" htmlType="button" disabled={!verbs.manage} onClick={() => setDialog("mapping")}>
              Edit mapping
            </Button>
            <Button type="empty" htmlType="button" disabled={!verbs.manage} onClick={() => setDialog("rotate")}>
              Rotate credential
            </Button>
            <Button type="danger" htmlType="button" disabled={!verbs.manage} onClick={() => setDialog("retire")}>
              Retire
            </Button>
            {verbs.reason && <span className="text-xs text-text-muted basis-full">{verbs.reason}</span>}
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
