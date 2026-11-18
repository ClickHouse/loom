import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  addQueryHistory,
  listQueryHistory,
  removeQueryHistory,
  type QueryHistorySources as Sources,
} from "../api/queryHistory";
import { IconButton } from "../components/IconButton";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";
import { BusyButton } from "../components/ui/BusyButton";
import { ListState } from "../components/ui/ListState";
import { Loading } from "../components/ui/Loading";
import { RowMenu } from "../components/ui/RowMenu";
import { SectionHeading } from "../components/ui/SectionHeading";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect, useMounted } from "../lib/hooks";

type Scope = { org: string; namespace: string };

export function QueryHistorySources({ org, namespace, panel }: Scope & { panel?: HTMLElement | null }) {
  const [open, setOpen] = useState(false);
  const disclosure = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const headingId = useId();
  const phone = useMedia(PHONE_QUERY);
  useEffect(() => {
    if (open && panel) document.getElementById(headingId)?.focus();
  }, [open, panel, headingId]);
  const toggle = {
    ref: disclosure,
    "aria-expanded": open,
    "aria-controls": panel !== undefined ? panelId : undefined,
    onClick: () => setOpen(!open),
  };
  const button =
    phone && panel !== undefined ? (
      <IconButton icon="database" label="Query history" {...toggle} />
    ) : (
      <Button type="empty" htmlType="button" {...toggle}>
        Query history
      </Button>
    );
  const list = open && (
    <SourceList
      key={`${org}/${namespace}`}
      org={org}
      namespace={namespace}
      focusDisclosure={() => disclosure.current?.focus()}
    />
  );
  if (panel === undefined) {
    return (
      <section className="mb-4" aria-label="Query history">
        {button}
        {list}
      </section>
    );
  }
  return (
    <>
      {button}
      {panel &&
        list &&
        createPortal(
          <section id={panelId} aria-labelledby={headingId} className="mb-6">
            <SectionHeading id={headingId} label="Query history" level={2} focusable />
            {list}
          </section>,
          panel,
        )}
    </>
  );
}

function SourceList({ org, namespace, focusDisclosure }: Scope & { focusDisclosure: () => void }) {
  const scope = { org, namespace };
  const mounted = useMounted();
  const [data, setData] = useState<Sources | null>(null);
  const [readError, setReadError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [writeError, setWriteError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [form, setForm] = useState(false);
  const [revision, setRevision] = useState(0);
  const connectButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef(false);
  useEffect(() => {
    if (!form && returnFocus.current) {
      connectButton.current?.focus();
      returnFocus.current = false;
    }
  }, [form]);
  function closeForm() {
    returnFocus.current = true;
    setForm(false);
  }
  const reload = () => setRevision((n) => n + 1);
  useAbortableEffect(
    async (signal, live) => {
      setReadError(null);
      setLoading(true);
      try {
        const response = await listQueryHistory({ org, namespace }, { signal });
        if (live()) setData(response);
      } catch (e) {
        if (live()) setReadError(e);
      } finally {
        if (live()) setLoading(false);
      }
    },
    [org, namespace, revision],
  );
  async function disconnect(name: string) {
    if (busy) return;
    setBusy(name);
    setWriteError("");
    try {
      await removeQueryHistory(scope, name);
      if (mounted.current) {
        reload();
        if (connectButton.current) connectButton.current.focus();
        else focusDisclosure();
      }
    } catch (e) {
      if (mounted.current) setWriteError(humanizeError(e, { bare: true }));
    } finally {
      if (mounted.current) setBusy(null);
    }
  }
  return (
    <div className="mt-2 rounded-lg border border-border p-3" aria-busy={data !== null && loading}>
      <p className="text-sm text-text-muted">
        Frequently used SELECT queries become private definition drafts. An admin approves each draft before it is
        published.
      </p>
      {readError ? (
        <ListState kind="error" things="query-history sources" error={readError} onRetry={reload} />
      ) : !data ? (
        <Loading label="Loading query-history sources" />
      ) : (
        <>
          {!data.enabled && (
            <p className="text-sm text-text-muted">
              An operator can enable query history for this deployment. Disconnecting a source remains available.
            </p>
          )}
          {data.sources.length === 0 && !form && (
            <ListState
              kind="empty"
              icon="database"
              title="No query-history sources"
              description="Connect a team's ClickHouse to draft definitions from its query history."
            />
          )}
          {data.sources.length > 0 && (
            <ul className="m-0 list-none p-0">
              {data.sources.map((source) => (
                <li
                  key={source.source_id}
                  className="flex items-center justify-between gap-3 border-b border-border py-2"
                >
                  <div className="min-w-0">
                    <span className="break-words text-sm text-text">{source.name}</span>
                    <p className="m-0 break-words text-xs text-text-muted">
                      {source.host}:{source.port} · {source.databases.join(", ")}
                    </p>
                  </div>
                  <RowMenu
                    name={source.name}
                    items={[
                      {
                        label: busy === source.name ? "Disconnecting…" : "Disconnect",
                        danger: true,
                        disabled: busy !== null,
                        onSelect: () => void disconnect(source.name),
                      },
                    ]}
                  />
                </li>
              ))}
            </ul>
          )}
          {data.enabled && !form && (
            <Button ref={connectButton} type="secondary" htmlType="button" onClick={() => setForm(true)}>
              Connect query history
            </Button>
          )}
          {form && (
            <SourceForm
              org={org}
              namespace={namespace}
              onCancel={closeForm}
              onDone={() => {
                closeForm();
                reload();
              }}
            />
          )}
        </>
      )}
      {writeError && (
        <p role="alert" className="text-sm text-text-muted">
          Couldn't disconnect this source — {writeError}
        </p>
      )}
    </div>
  );
}

function SourceForm({ org, namespace, onDone, onCancel }: Scope & { onDone: () => void; onCancel: () => void }) {
  const id = useId();
  const formElement = useRef<HTMLFormElement>(null);
  useEffect(() => {
    formElement.current?.querySelector("input")?.focus();
  }, []);
  const mounted = useMounted();
  const [form, setForm] = useState({
    name: "",
    host: "",
    port: "8443",
    user: "",
    password: "",
    databases: "",
    cluster: "",
    secure: true,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function field(key: keyof typeof form, value: string | boolean) {
    setForm((current) => ({ ...current, [key]: value }));
  }
  async function submit() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await addQueryHistory(
        { org, namespace },
        {
          ...form,
          port: Number(form.port),
          databases: form.databases
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        },
      );
      if (mounted.current) onDone();
    } catch (e) {
      if (mounted.current) setError(humanizeError(e, { bare: true }));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <form
      ref={formElement}
      className="mt-3 space-y-3"
      aria-label="Connect query history"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <TextField
          id={`${id}-name`}
          label="Source name"
          value={form.name}
          onChange={(v) => field("name", v)}
          disabled={busy}
          required
        />
        <TextField
          id={`${id}-host`}
          label="ClickHouse host"
          value={form.host}
          onChange={(v) => field("host", v)}
          disabled={busy}
          required
        />
        <TextField
          id={`${id}-port`}
          label="Port"
          value={form.port}
          onChange={(v) => field("port", v)}
          disabled={busy}
          required
        />
        <Checkbox
          label="Use TLS"
          checked={form.secure}
          onCheckedChange={(v) => field("secure", v === true)}
          disabled={busy}
        />
        <TextField
          id={`${id}-user`}
          label="Read-only username"
          value={form.user}
          onChange={(v) => field("user", v)}
          disabled={busy}
          required
        />
        <PasswordField
          id={`${id}-password`}
          label="Password"
          value={form.password}
          onChange={(v) => field("password", v)}
          autoComplete="new-password"
          disabled={busy}
        />
        <TextField
          id={`${id}-databases`}
          label="Databases (comma separated)"
          value={form.databases}
          onChange={(v) => field("databases", v)}
          disabled={busy}
          required
        />
        <TextField
          id={`${id}-cluster`}
          label="Cluster (optional)"
          value={form.cluster}
          onChange={(v) => field("cluster", v)}
          disabled={busy}
        />
      </div>
      <p className="text-xs text-text-muted">
        Only queries entirely within these databases are read. The account needs read access to system.query_log; its
        password is stored encrypted.
      </p>
      {error && (
        <p role="alert" className="text-sm text-text-muted">
          Couldn't connect query history — {error}
        </p>
      )}
      <div className="flex gap-2">
        <BusyButton busy={busy} htmlType="submit">
          Connect
        </BusyButton>
        <Button type="secondary" htmlType="button" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
