import { useEffect, useMemo, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Table } from "@clickhouse/click-ui/Table";
import { TextField } from "@clickhouse/click-ui/TextField";
import { useStore } from "../../lib/state";
import { hasPlatformAdminClaim } from "../../lib/role";
import {
  adminSettingReset,
  adminSettingSet,
  adminSettingsHistory,
  adminSettingsList,
  type SettingChange,
  type SettingRow,
} from "../../api";
import { EmptyState } from "../../components/EmptyState";
import { useAbortableEffect } from "../../lib/hooks";
import { ListPager, usePageSize } from "../../components/ui/ListPager";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { BusyButton } from "../../components/ui/BusyButton";
import { TableSkeleton } from "../../components/Skeleton";
import { toast, toastError } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { PageContainer } from "../../layout/PageContainer";
import { PageHeader, SectionHeading } from "../../components/ui";

const SOURCE_LABEL: Record<SettingRow["source"], string> = {
  override: "Override",
  dashboard: "Models & keys",
  env: "Environment",
  env_file: "Dotenv file",
  settings_file: "Settings file",
  inferred: "Inferred",
  default: "Default",
};

/** A value as the settings file would hold it. */
export function shown(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** What the edit field holds, as the server takes it: the text for a string setting
 *  (a namespace named `123` stays a string), else JSON when it parses, else the text,
 *  which the setting validates as its environment variable. */
export function parsed(text: string, row: Pick<SettingRow, "default">): unknown {
  if (typeof row.default === "string") return text;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** Off its default or overridden, as `settings.list changed` and `loom settings list
 *  --changed` mean it. */
export function changed(r: SettingRow): boolean {
  return r.source === "override" || shown(r.value) !== shown(r.default);
}

/** The settings a search keeps: by name or by what the description says. */
export function matching(rows: SettingRow[], query: string, changedOnly: boolean, liveOnly: boolean): SettingRow[] {
  const q = query.trim().toLowerCase();
  return rows.filter(
    (r) =>
      (!changedOnly || changed(r)) &&
      (!liveOnly || r.live) &&
      (!q || r.name.includes(q) || r.description.toLowerCase().includes(q)),
  );
}

/**
 * Deployment settings — platform admins only, as `/v1/admin/settings.*` is. Every
 * setting's value in force, where it came from and whether it is live; a live one takes
 * a deployment-wide override here, refused when someone changed it meanwhile.
 */
export function DeploymentSettings() {
  const whoami = useStore((s) => s.whoami);
  const [rows, setRows] = useState<SettingRow[] | null>(null);
  const [changes, setChanges] = useState<SettingChange[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [query, setQuery] = useState("");
  const [changedOnly, setChangedOnly] = useState(true);
  const [liveOnly, setLiveOnly] = useState(false);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = usePageSize("deployment-settings");
  const [reads, setReads] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const allowed = hasPlatformAdminClaim(whoami);

  useAbortableEffect(
    (signal, live) => {
      if (!allowed) return;
      setError(null);
      setRefreshing(true);
      Promise.all([adminSettingsList({ signal }), adminSettingsHistory("", 20, { signal })])
        .then(([list, history]) => {
          if (!live()) return;
          setRows(list);
          setChanges(history);
        })
        .catch((e) => {
          if (live()) setError(e);
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [allowed, reads],
  );

  const shownRows = useMemo(
    () => matching(rows ?? [], query, changedOnly, liveOnly),
    [rows, query, changedOnly, liveOnly],
  );
  // One array per list read and filter: the table waits for a new one to place focus.
  const pageRows = useMemo(() => shownRows.slice(page * pageSize, (page + 1) * pageSize), [shownRows, page, pageSize]);

  if (!allowed) {
    return (
      <PageContainer>
        <EmptyState
          icon="secure"
          title="Platform admins only"
          description="Deployment settings apply to every org on this Loom, so only a platform admin token may read or change them."
        />
      </PageContainer>
    );
  }

  const refresh = () => setReads((n) => n + 1);
  return (
    <PageContainer>
      <div className="mb-6">
        <PageHeader
          title="Deployment settings"
          actions={
            <BusyButton type="secondary" htmlType="button" iconLeft="refresh" onClick={refresh} busy={refreshing}>
              Refresh
            </BusyButton>
          }
        />
        <p className="text-sm text-text-muted max-w-2xl">
          The value every setting has on this replica and where it came from. A live setting takes an override here that
          every replica applies within seconds and that outlives a restart; the others change with the environment or
          the settings file and a restart.
        </p>
      </div>

      <ListToolbar
        label="Filter settings"
        count={rows ? `${shownRows.length.toLocaleString()} of ${rows.length.toLocaleString()}` : undefined}
      >
        <div className="min-w-0 flex-1">
          <SearchField
            value={query}
            onChange={(v) => {
              setQuery(v);
              setPage(0);
            }}
            placeholder="Filter by name or description — e.g. memory_ask_, recency…"
            aria-label="Filter settings"
          />
        </div>
        <Checkbox
          label="Changed only"
          checked={changedOnly}
          onCheckedChange={(x) => {
            setChangedOnly(x === true);
            setPage(0);
          }}
        />
        <Checkbox
          label="Live only"
          checked={liveOnly}
          onCheckedChange={(x) => {
            setLiveOnly(x === true);
            setPage(0);
          }}
        />
      </ListToolbar>

      {!!error && <ListState kind="error" things="settings" error={error} onRetry={refresh} />}
      {!error && rows === null && (
        <Table
          aria-label="Settings"
          aria-busy
          size="sm"
          headers={HEADERS}
          rows={[]}
          noDataMessage={<TableSkeleton label="Loading settings" />}
        />
      )}
      {!error && rows !== null && (
        <div className="space-y-2" aria-busy={refreshing || undefined}>
          <SettingsTable rows={pageRows} onChanged={refresh} />
          <ListPager
            label="Settings pages"
            page={page}
            pageSize={pageSize}
            shown={pageRows.length}
            total={shownRows.length}
            busy={refreshing}
            onPage={setPage}
            onPageSize={(n) => {
              setPageSize(n);
              setPage(0);
            }}
            topId="settings-list"
          />
        </div>
      )}

      {rows !== null && <RecentChanges changes={changes} />}
    </PageContainer>
  );
}

const HEADERS = [{ label: "Setting" }, { label: "Value" }, { label: "Source" }, { label: "" }];

/** The id of a row's control, so focus can land on it once it renders. */
const controlId = (kind: "change" | "value", name: string) => `setting-${kind}-${name}`;

function SettingsTable({ rows, onChanged }: { rows: SettingRow[]; onChanged: () => void }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  // The value the edit started from: a Save sends it as `expected`, so a change made
  // elsewhere while the field was open is refused even when a Refresh shows it.
  const [expected, setExpected] = useState<SettingRow["value"]>(null);
  const [busy, setBusy] = useState(false);
  // The control that takes focus once it renders: the edit field when Change opens it,
  // the row's Change button when Save, Cancel or Reset takes the focused one away. After
  // a save or a reset it waits for the refreshed rows (`after` is the list it started
  // from): under "Changed only" a reset to the default takes the row off the list, and
  // focus then goes to the list itself.
  const [focusNext, setFocusNext] = useState<{ id: string; after?: SettingRow[] } | null>(null);
  useEffect(() => {
    if (!focusNext || rows === focusNext.after) return;
    const el = document.getElementById(focusNext.id) ?? document.getElementById("settings-list");
    el?.focus();
    setFocusNext(null);
  });
  const close = (name: string, refreshed: boolean) => {
    setEditing(null);
    setFocusNext({ id: controlId("change", name), after: refreshed ? rows : undefined });
  };

  const save = async (row: SettingRow) => {
    setBusy(true);
    try {
      const after = await adminSettingSet(row.name, parsed(draft, row), expected);
      toast(`${row.name} is ${shown(after.value)} on every replica within seconds`, "ok");
      close(row.name, true);
      onChanged();
    } catch (e) {
      toastError(`${row.name} not changed`, e);
    } finally {
      setBusy(false);
    }
  };
  const reset = async (row: SettingRow) => {
    if (
      !(await confirmAction({
        title: `Reset ${row.name}?`,
        message: "Every replica goes back to the value its environment or settings file gives, or the default.",
        confirmLabel: "Reset",
      }))
    )
      return;
    try {
      await adminSettingReset(row.name, row.value);
      setFocusNext({ id: controlId("change", row.name), after: rows });
      onChanged();
    } catch (e) {
      toastError(`${row.name} not reset`, e);
    }
  };

  return (
    <Table
      id="settings-list"
      tabIndex={-1}
      aria-label="Settings"
      size="sm"
      noDataMessage="No setting matches."
      headers={HEADERS}
      rows={rows.map((r) => ({
        id: r.name,
        items: [
          {
            label: (
              <div>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="font-mono text-sm">{r.name}</span>
                  {r.live && <Badge text="Live" state="info" size="sm" />}
                </div>
                {r.description && <div className="text-xs text-text-muted mt-0.5 max-w-xl">{r.description}</div>}
              </div>
            ),
            overflowMode: "wrap",
          },
          {
            label:
              editing === r.name ? (
                <TextField
                  id={controlId("value", r.name)}
                  aria-label={`New value for ${r.name}`}
                  value={draft}
                  onChange={(v) => setDraft(v)}
                  className="min-w-40"
                />
              ) : (
                <div>
                  <span className="font-mono text-sm break-all">{shown(r.value)}</span>
                  {shown(r.value) !== shown(r.default) && (
                    <div className="text-xs text-text-muted">default {shown(r.default)}</div>
                  )}
                </div>
              ),
            overflowMode: "wrap",
          },
          { label: <span className="text-xs text-text-muted">{SOURCE_LABEL[r.source]}</span> },
          {
            label: !r.live ? null : editing === r.name ? (
              <div className="flex gap-2">
                <Button type="primary" htmlType="button" loading={busy} onClick={() => void save(r)}>
                  Save
                </Button>
                <Button type="secondary" htmlType="button" onClick={() => close(r.name, false)}>
                  Cancel
                </Button>
              </div>
            ) : (
              <div className="flex gap-2">
                <Button
                  id={controlId("change", r.name)}
                  type="secondary"
                  htmlType="button"
                  aria-label={`Change ${r.name}`}
                  onClick={() => {
                    setEditing(r.name);
                    setDraft(shown(r.value));
                    setExpected(r.value);
                    setFocusNext({ id: controlId("value", r.name) });
                  }}
                >
                  Change
                </Button>
                {r.source === "override" && (
                  <Button
                    type="secondary"
                    htmlType="button"
                    aria-label={`Reset ${r.name}`}
                    onClick={() => void reset(r)}
                  >
                    Reset
                  </Button>
                )}
              </div>
            ),
          },
        ],
      }))}
    />
  );
}

/** Only an override is the deployment's value to set back; anything else but null (the
 *  reset of a setting this build lacks, which has no undo) was the changing process's
 *  own, which a reset restores (as `loom settings history` marks it). */
export function undoIsReset(c: SettingChange): boolean {
  return c.previous_source !== null && c.previous_source !== "override";
}

const CHANGE_HEADERS = [{ label: "When" }, { label: "Who" }, { label: "Setting" }, { label: "Change" }];

function RecentChanges({ changes }: { changes: SettingChange[] }) {
  return (
    <section className="mt-8">
      <SectionHeading label="Recent changes" />
      <Table
        aria-label="Recent setting changes"
        size="sm"
        noDataMessage="No setting has been changed here yet."
        headers={CHANGE_HEADERS}
        rows={changes.map((c, i) => ({
          id: `${c.ts}-${c.name}-${i}`,
          items: [
            { label: <ListTime time={c.ts} quiet /> },
            { label: <span className="font-mono text-xs">{c.actor}</span> },
            { label: <span className="font-mono text-sm">{c.name}</span> },
            {
              label: (
                <span className="font-mono text-xs">
                  {shown(c.previous)}
                  {undoIsReset(c) ? " (undo: reset)" : ""} → {shown(c.value)}
                  {c.cleared ? " (reset)" : ""}
                </span>
              ),
            },
          ],
        }))}
      />
    </section>
  );
}
