import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { TextField } from "@clickhouse/click-ui/TextField";
import { lazy, Suspense, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  type MCPSource, type MCPSourceSummary, mcpControl, mcpInspect, mcpList, mcpRemove,
} from "../api/mcpSources";
import { Alert } from "../components/ui/Alert";
import { ListRow } from "../components/ui/ListRow";
import { ListState } from "../components/ui/ListState";
import { Loading } from "../components/ui/Loading";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";
import { DialogFrame } from "../flows/TaskDialogFrame";
import { mcpSourceCopy as copy } from "../flows/mcpSourceCopy";
import { useMCPRequest } from "../flows/useMCPRequest";
import { MCPLiveRead } from "../flows/MCPLiveRead";
import { canCallOwnConnections } from "../lib/canCall";
import { onCollectionsChanged } from "../lib/collectionPlaces";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import { needsRole, useNamespaceRoleRead } from "../lib/namespaceRole";
import { useStore } from "../lib/state";
import { useRowNav } from "../lib/useRowNav";

const MCPSourceSetup = lazy(() => import("../flows/MCPSourceSetup").then((m) => ({ default: m.MCPSourceSetup })));
const MCPConnectionsDialog = lazy(() => import("../flows/MCPConnectionStep").then((m) => ({ default: m.MCPConnectionsDialog })));

/** Mounted by the Library and repository Sources entry point, independently of native adapters. */
export function MCPSources({ org, namespace, section, panel, busy = false }: {
  org: string; namespace: string; section?: string; panel?: HTMLElement | null; busy?: boolean;
}) {
  const whoami = useStore((s) => s.whoami);
  const identityOrg = useStore((s) => s.identity?.org);
  const authed = useStore((s) => s.authed);
  const expired = useStore((s) => s.sessionExpired);
  const role = useNamespaceRoleRead(namespace);
  const [open, setOpen] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const headingId = useId();
  const panelId = useId();
  const allowed = authed && !expired && identityOrg === org && whoami?.org === org && canCallOwnConnections(whoami);
  const writer = role.role === "writer" || role.role === "admin";
  const callerKey = JSON.stringify([org, whoami?.subject]);
  const collectionVersion = JSON.stringify([whoami, role.rows]);
  useEffect(() => { if (open) heading.current?.focus(); }, [open]);
  const content = <section id={panelId} aria-labelledby={headingId} className="space-y-3 mb-4">
    <h2 ref={heading} tabIndex={-1} id={headingId} className="text-sm font-semibold">{copy.title}</h2>
    {!allowed ? <p>Sign in with your own user account to manage your MCP sources.</p> : !writer ?
      <p>{role.loading ? "Checking your collection role…" : needsRole("writer", namespace)}</p> :
      <SourceRegistrations key={`${callerKey}/${namespace}`} org={org} namespace={namespace} callerKey={callerKey} collectionVersion={collectionVersion} disabled={busy} />}
  </section>;
  if (!section) return content;
  return <>
    <Button type="empty" htmlType="button" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen((value) => !value)}>MCP sources</Button>
    {open && panel && createPortal(content, panel)}
  </>;
}

function SourceRegistrations({ org, namespace, callerKey, collectionVersion, disabled }: {
  org: string; namespace: string; callerKey: string; collectionVersion: string; disabled: boolean;
}) {
  const [cursor, setCursor] = useState<string | null>(null);
  const [previous, setPrevious] = useState<(string | null)[]>([]);
  const [revision, setRevision] = useState(0);
  const [authorityEpoch, setAuthorityEpoch] = useState(0);
  const [answer, setAnswer] = useState<Awaited<ReturnType<typeof mcpList>> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [adding, setAdding] = useState(false);
  const [connectionsOpen, setConnectionsOpen] = useState(false);
  const [details, setDetails] = useState<MCPSource | null>(null);
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState("");
  const top = useRef<HTMLDivElement>(null);
  const request = useMCPRequest(JSON.stringify([org, namespace, callerKey, collectionVersion, authorityEpoch, cursor]));
  const rowNav = useRowNav({ enabled: !details && !adding && !connectionsOpen });
  const reload = () => { setLoading(true); setCursor(null); setPrevious([]); setRevision((value) => value + 1); };
  const refreshRef = useRef(() => {});
  refreshRef.current = () => { setAuthorityEpoch((v) => v + 1); reload(); };
  useEffect(() => {
    const off = onCollectionsChanged((value) => { if (!value || value.org === org) refreshRef.current(); });
    const focus = () => refreshRef.current();
    window.addEventListener("focus", focus);
    return () => { off(); window.removeEventListener("focus", focus); };
  }, [org]);
  useAbortableEffect(async (signal, live) => {
    setLoading(true); setError(null);
    try {
      const value = await mcpList(org, namespace, cursor, signal);
      if (live()) { setAnswer(value); setLoading(false); }
    } catch (failure) { if (live()) { setError(failure); setLoading(false); } }
  }, [org, namespace, callerKey, cursor, revision, collectionVersion]);
  const inspect = async (row: MCPSourceSummary) => {
    if (disabled || loading || request.busy) return;
    const result = await request.run((signal) => mcpInspect({ org, namespace, name: row.name }, signal));
    if (result?.found && result.source?.name === row.name && result.source.namespace === namespace) { setDetails(result.source); setEditing(false); }
    else if (result) { setNotice("No owned source was returned. Refresh this collection's registrations."); reload(); }
  };
  const saved = (source: MCPSource) => { setNotice(`Server source ${source.name} in ${source.namespace}: ${source.state}. Refresh state: ${source.refresh_state}.`); reload(); };
  const nextCursor = answer?.next_cursor;
  const repeated = !!nextCursor && (nextCursor === cursor || previous.includes(nextCursor));
  const page = (next: string | null, back: boolean) => {
    if (loading || request.busy) return;
    if (back) setPrevious((values) => values.slice(0, -1)); else setPrevious((values) => [...values, cursor]);
    setLoading(true); setAnswer(null); setCursor(next); top.current?.focus();
  };
  return <div ref={top} tabIndex={-1} className="space-y-3" aria-busy={loading || request.busy}>
    <p className="text-sm">Your registrations in {namespace}, 20 per page. Other owners' registrations and resource IDs are not listed.</p>
    <p className="text-xs text-text-muted">{copy.retained}</p>
    <div className="flex flex-wrap gap-2">
      <Button type="primary" htmlType="button" disabled={disabled || adding || !!details || connectionsOpen} onClick={() => setAdding(true)}>Set up MCP source</Button>
      <Button type="secondary" htmlType="button" disabled={disabled || adding || !!details || connectionsOpen} onClick={() => setConnectionsOpen(true)}>Manage MCP connections</Button>
      <Button type="secondary" htmlType="button" disabled={loading || disabled} onClick={() => refreshRef.current()}>Refresh registrations</Button>
    </div>
    {loading && <Loading label="Loading your MCP source registrations" />}
    {!!error && <ListState kind="error" things="MCP source registrations" error={error} onRetry={reload} />}
    {!loading && !error && answer?.sources.length === 0 && <ListState kind="empty" icon="connect" title="No owned MCP registrations on this page" description="Set up an MCP source to register a fixed selection in an ordinary collection." />}
    {!error && <div ref={rowNav} aria-label="Your MCP registrations" className="space-y-2">
      {(answer?.sources ?? []).map((source) => <ListRow key={source.name}>
        <Button type="empty" htmlType="button" data-row-open disabled={loading || disabled || request.busy} onClick={() => void inspect(source)}>{source.label || source.name}</Button>
        <p className="text-xs text-text-muted">{source.state} · {source.mode} · {source.reader_policy} · refresh {source.refresh_state} · local authority {source.local_authority}</p>
      </ListRow>)}
    </div>}
    <div className="flex flex-wrap gap-2">
      {previous.length > 0 && <Button type="secondary" htmlType="button" disabled={loading} onClick={() => page(previous[previous.length - 1] ?? null, true)}>Previous registrations</Button>}
      {nextCursor && <Button type="secondary" htmlType="button" disabled={loading || repeated} onClick={() => page(nextCursor, false)}>Load more registrations</Button>}
    </div>
    {repeated && <p role="status">The server repeated a cursor. Refresh registrations before paging further.</p>}
    {!!request.error && <Alert role="alert" state="danger" text={humanizeError(request.error, { read: true })} />}
    {notice && <p role="status">{notice}</p>}
    {adding && <Suspense fallback={<Loading label="Loading source setup" />}><MCPSourceSetup org={org} callerKey={callerKey} collectionVersion={`${collectionVersion}/${authorityEpoch}`} onSaved={saved} onClose={() => setAdding(false)} /></Suspense>}
    {connectionsOpen && <Suspense fallback={<Loading label="Loading MCP connection management" />}><MCPConnectionsDialog org={org} callerKey={callerKey} contextKey={`${collectionVersion}/${authorityEpoch}`} onClose={() => { setConnectionsOpen(false); refreshRef.current(); }} /></Suspense>}
    {details && !editing && <MCPSourceDetails key={details.name} org={org} source={details} contextKey={`${callerKey}/${collectionVersion}/${authorityEpoch}`} onClose={() => setDetails(null)}
      onChanged={(source) => { setDetails(source); saved(source); }} onEdit={() => setEditing(true)} />}
    {details && editing && <Suspense fallback={<Loading label="Loading source editor" />}><MCPSourceSetup org={org} callerKey={callerKey} collectionVersion={`${collectionVersion}/${authorityEpoch}`} source={details}
      onSaved={(source) => { setDetails(source); saved(source); }} onClose={() => setEditing(false)} /></Suspense>}
  </div>;
}

function MCPSourceDetails({ org, source, contextKey, onClose, onChanged, onEdit }: {
  org: string; source: MCPSource; contextKey: string; onClose: () => void; onChanged: (value: MCPSource) => void; onEdit: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [remove, setRemove] = useState(false);
  const [confirm, setConfirm] = useState("");
  const [erase, setErase] = useState(false);
  const [eraseConfirm, setEraseConfirm] = useState("");
  const [notice, setNotice] = useState("");
  const request = useMCPRequest(contextKey);
  const key = { org, namespace: source.namespace, name: source.name };
  const mutate = async (action: "pause" | "resume" | "refresh" | "remove", intent: "automatic" | "explicit") => {
    if (pending || request.busy || (action === "remove" && (confirm !== source.name || (erase && eraseConfirm !== "ERASE")))) return;
    setPending(true);
    const result = await request.run((signal) => action === "remove" ?
      mcpRemove({ ...key, confirm, delete_documents: erase }, signal, intent) : mcpControl(key, action, signal, intent));
    if (result) {
      setPending(false); setRemove(false); setErase(false); setConfirm(""); setEraseConfirm(""); onChanged(result.source);
      setNotice(`Server state: ${result.source.state}. Copies: ${result.copies}. Refresh requested: ${result.refresh_requested ? "yes" : "no"}.`);
    }
  };
  const reconcile = async () => {
    const result = await request.run((signal) => mcpInspect(key, signal));
    if (result?.found && result.source?.name === source.name && result.source.namespace === source.namespace) {
      onChanged(result.source);
      setNotice("Current server state inspected. This inspection does not attest completion of the original mutation; resubmission remains blocked.");
    } else if (result) setNotice("No owned registration was returned. Do not repeat the mutation.");
  };
  const items: RowMenuItem[] = [
    { label: "Edit selection", disabled: pending || request.busy || source.state === "retired" || source.state === "unsupported", onSelect: onEdit },
    { label: "Pause", disabled: pending || request.busy || source.state !== "registered", onSelect: () => void mutate("pause", "explicit") },
    { label: "Resume", disabled: pending || request.busy || source.state !== "paused", onSelect: () => void mutate("resume", "explicit") },
    { label: "Refresh selected items", disabled: pending || request.busy || source.mode === "live" || source.state !== "registered", onSelect: () => void mutate("refresh", "explicit") },
    { label: "Remove registration", danger: true, disabled: pending || request.busy || source.state === "retired", onSelect: () => setRemove(true) },
  ];
  return <DialogFrame open onOpenChange={(open) => { if (!open && !request.busy && !pending) onClose(); }} title="MCP source registration" description="Current server registration metadata; upstream permissions remain unsupported."
    footer={<Button type="secondary" htmlType="button" disabled={request.busy} onClick={onClose}>{pending ? "Close and discard this pending result" : "Close"}</Button>}>
    <div className="flex gap-2 items-start"><p className="flex-1 text-sm">{source.label || source.name}</p><RowMenu name={source.name} items={items} /></div>
    <p className="text-sm">Name: {source.name}. State: {source.state}. Mode: {source.mode}. Audience: {source.reader_policy}. Revision: {source.scope_revision}. Refresh: {source.refresh_state}. Local authority: {source.local_authority}. Pending erasure: {source.pending_erasure ? "yes" : "no"}.</p>
    {source.rule && <p className="text-sm">Server access: {source.rule.access}. {source.rule.reason} {source.rule.explanation}</p>}
    <ul aria-label="Selected opaque resource IDs" className="text-xs font-mono break-words">{source.resource_ids.map((id, index) => <li key={`${index}:${id}`}>{id}</li>)}</ul>
    <p className="text-sm">{copy.retained}</p>
    <p className="text-sm">Read evidence expires after {source.read_evidence_stale_seconds} seconds. Snapshot access expires without manual refresh while bytes remain retained. Both audiences require current authorization and fresh evidence.</p>
    {source.mode === "live" && source.reader_policy === "owner_only" && source.state === "registered" && <MCPLiveRead key={`${source.name}/${contextKey}/${source.scope_revision}`} org={org} source={source} contextKey={contextKey} disabled={pending || request.busy} />}
    {remove && <fieldset disabled={pending || request.busy} className="space-y-2">
      <p>Remove this registration. Stored copies are retained by default; access still requires current authorization.</p>
      <TextField label="Repeat the exact registration name to remove" value={confirm} onChange={setConfirm} />
      <Checkbox label="Also request erasure of stored copies" checked={erase} onCheckedChange={(value) => { setErase(value === true); setEraseConfirm(""); }} />
      {erase && <TextField label="Type ERASE to confirm asynchronous copy erasure" value={eraseConfirm} onChange={setEraseConfirm} />}
      <Button type="primary" htmlType="button" disabled={confirm !== source.name || (erase && eraseConfirm !== "ERASE")} onClick={() => void mutate("remove", "explicit")}>Confirm removal</Button>
      <Button type="secondary" htmlType="button" onClick={() => setRemove(false)}>Cancel removal</Button>
    </fieldset>}
    {pending && <><Alert state="warning" text={copy.uncertain} /><Button type="secondary" htmlType="button" disabled={request.busy} onClick={() => void reconcile()}>Inspect current source state</Button></>}
    {!!request.error && <Alert role="alert" state="danger" text={humanizeError(request.error)} />}
    {notice && <p role="status">{notice}</p>}
  </DialogFrame>;
}
