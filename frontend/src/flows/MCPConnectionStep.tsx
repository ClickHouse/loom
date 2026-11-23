import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { useEffect, useRef, useState } from "react";
import {
  type MCPConnection, type MCPDiscovery, type MCPProfile, mcpConnect, mcpConnections,
  mcpDiscover, mcpDisconnect, mcpInspectConnection, mcpProfiles,
} from "../api/mcpSources";
import { Alert } from "../components/ui/Alert";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { ListState } from "../components/ui/ListState";
import { Loading } from "../components/ui/Loading";
import { useAbortableEffect } from "../lib/hooks";
import type { DashboardRequestIntent } from "../lib/dashboardTransport";
import { mcpSourceCopy as copy } from "./mcpSourceCopy";
import { DialogFrame } from "./TaskDialogFrame";
import { useMCPRequest } from "./useMCPRequest";

export interface MCPChosenConnection { connection: MCPConnection; discovery: MCPDiscovery }

export function MCPConnectionsDialog({ org, callerKey, contextKey, onClose }: { org: string; callerKey: string; contextKey: string; onClose: () => void }) {
  const [uncertain, setUncertain] = useState(false);
  return <DialogFrame open title="Manage MCP connections" description="Only your connections are listed. Disconnecting requires separate confirmation."
    onOpenChange={(open) => { if (!open && !uncertain) onClose(); }} footer={<Button type="secondary" htmlType="button" onClick={onClose}>{uncertain ? "Close and discard this in-memory connection receipt" : "Close"}</Button>}>
    <MCPConnectionStep key={`${org}/${callerKey}`} org={org} callerKey={callerKey} contextKey={contextKey} onChange={() => {}} onUncertainChange={setUncertain} />
    <p className="text-xs text-text-muted">{copy.privacy}</p>
  </DialogFrame>;
}

/** Endpoint disclosure precedes credential entry. Discovery proves capabilities, never remote identity. */
export function MCPConnectionStep({ org, callerKey, contextKey = "", onChange, onUncertainChange, disabled = false }: {
  org: string; callerKey: string; onChange: (value: MCPChosenConnection | null) => void;
  contextKey?: string; onUncertainChange?: (value: boolean) => void; disabled?: boolean;
}) {
  const [profileCursor, setProfileCursor] = useState<string | null>(null);
  const [connectionCursor, setConnectionCursor] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [catalog, setCatalog] = useState<Awaited<ReturnType<typeof mcpProfiles>> | null>(null);
  const [accounts, setAccounts] = useState<Awaited<ReturnType<typeof mcpConnections>> | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [profile, setProfile] = useState<MCPProfile | null>(null);
  const [slot, setSlot] = useState("");
  const [bearer, setBearer] = useState("");
  const [connectionLabel, setConnectionLabel] = useState("");
  const [pending, setPending] = useState<{ request_id: string; profile_id: string } | null>(null);
  const [disconnectConfirmed, setDisconnectConfirmed] = useState(false);
  const [disconnectUncertain, setDisconnectUncertain] = useState(false);
  const [disconnectSlot, setDisconnectSlot] = useState("");
  const [notice, setNotice] = useState("");
  const [discovered, setDiscovered] = useState<{ key: string; value: MCPDiscovery } | null>(null);
  useEffect(() => { onUncertainChange?.(pending !== null || disconnectUncertain); }, [pending, disconnectUncertain, onUncertainChange]);
  const scope = JSON.stringify([org, callerKey, contextKey, profile?.profile_sha256, slot, revision]);
  const request = useMCPRequest(scope);
  const refresh = () => {
    onChange(null); setBearer(""); setProfile(null); setSlot(""); setCatalog(null); setAccounts(null);
    setLoading(true); setProfileCursor(null); setConnectionCursor(null); setRevision((r) => r + 1);
  };
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const previousContext = useRef(contextKey);
  useEffect(() => {
    if (previousContext.current !== contextKey) {
      previousContext.current = contextKey;
      refreshRef.current();
    }
  }, [contextKey]);
  useEffect(() => {
    const focus = () => refreshRef.current();
    window.addEventListener("focus", focus);
    return () => window.removeEventListener("focus", focus);
  }, []);
  useAbortableEffect(async (signal, live) => {
    setLoading(true); setLoadError(null);
    try {
      const [profiles, connections] = await Promise.all([
        mcpProfiles(org, profileCursor, signal), mcpConnections(org, connectionCursor, signal),
      ]);
      if (!live()) return;
      setCatalog(profiles); setAccounts(connections); setLoading(false);
    } catch (error) { if (live()) { setLoadError(error); setLoading(false); } }
  }, [org, callerKey, contextKey, profileCursor, connectionCursor, revision]);

  const useConnection = async (connection: MCPConnection, intent: DashboardRequestIntent) => {
    if (connection.state !== "configured" || !profile || !origin ||
      connection.profile_sha256 !== profile.profile_sha256) return;
    const result = await request.run(async (signal) => {
      const inspected = await mcpInspectConnection(org, { slot: connection.slot }, signal);
      const current = inspected.connection;
      if (!inspected.found || !current || current.state !== "configured" ||
        current.slot !== connection.slot || current.profile_id !== profile.id ||
        current.generation !== connection.generation || current.profile_sha256 !== profile.profile_sha256)
        throw new Error("Connection changed. Refresh the connection list.");
      const discovery = await mcpDiscover(org, current.slot, signal, intent);
      if (discovery.generation !== current.generation || discovery.slot !== current.slot || !discovery.supported_reads)
        throw new Error("This connection has no supported reads. Refresh or choose another connection.");
      return { connection: current, discovery };
    });
    if (result) {
      setDiscovered({ key: scope, value: result.discovery });
      onChange(result); setNotice("Capability discovery returned supported reads. Remote identity and ACLs remain unverified.");
    }
  };
  const connect = async (intent: DashboardRequestIntent) => {
    if (!profile || !origin || pending || !catalog?.self_service_enabled || request.busy || disabled || connectionLabel.length > 256) return;
    const receipt = { request_id: crypto.randomUUID(), profile_id: profile.id };
    setPending(receipt);
    const secret = profile.auth === "bearer" ? bearer : undefined;
    setBearer(""); onChange(null);
    const result = await request.run((signal) => mcpConnect({ org, ...receipt, ...(connectionLabel ? { label: connectionLabel } : {}), ...(secret === undefined ? {} : { bearer: secret }) }, signal, intent));
    if (result) {
      setPending(null); setSlot(result.connection.slot);
      setAccounts((a) => a ? { ...a, connections: [result.connection, ...a.connections.filter((c) => c.slot !== result.connection.slot)] } : a);
      setNotice(`Server connection state: ${result.connection.state}. Discover capabilities before selecting content.`);
    }
  };
  const reconcile = async () => {
    const inspectSlot = disconnectUncertain ? disconnectSlot : slot;
    if (!pending && !inspectSlot) return;
    const result = await request.run((signal) => mcpInspectConnection(org, pending ? { request_id: pending.request_id } : { slot: inspectSlot }, signal));
    if (!result) return;
    setNotice(`Server reconciliation: ${result.reconciliation}. ${result.connection ? `Connection state: ${result.connection.state}.` : "No confirmed connection was returned. Do not resubmit."}`);
    if (result.found && result.connection && result.reconciliation === "present") {
      const connection = result.connection;
      setPending(null);
      if (connection.state === "stopped") { setDisconnectUncertain(false); setDisconnectSlot(""); }
      setSlot(connection.slot);
      setAccounts((a) => a ? { ...a, connections: [connection, ...a.connections.filter((c) => c.slot !== connection.slot)] } : a);
    }
  };
  const disconnect = async (intent: DashboardRequestIntent) => {
    if (!slot || !disconnectConfirmed || disconnectUncertain || request.busy || disabled) return;
    onChange(null); setDisconnectSlot(slot); setDisconnectUncertain(true);
    const result = await request.run((signal) => mcpDisconnect(org, slot, signal, intent));
    if (result) {
      setDisconnectUncertain(false); setDisconnectSlot(""); setDisconnectConfirmed(false);
      setNotice(`Server disconnected: ${result.disconnected ? "yes" : "no"}. Upstream credential revoked: no. Revoke it with the upstream service yourself.`);
      setAccounts((a) => a ? { ...a, connections: a.connections.map((c) => c.slot === slot && result.disconnected ? { ...c, state: "stopped" } : c) } : a);
    }
  };
  // A missing origin is a catalog integration gap, never permission to guess a host.
  const origin = disclosedOrigin(profile?.endpoint_origin);
  const choices = (accounts?.connections ?? []).filter((c) => c.profile_id === profile?.id);
  const selected = choices.find((c) => c.slot === slot);
  const locked = disabled || request.busy || pending !== null || disconnectUncertain;
  return (
    <section aria-label="MCP connection" aria-busy={loading || request.busy} className="space-y-3">
      <p className="text-sm">{copy.transport}</p>
      {loading && <Loading label="Loading MCP profiles and your connections" />}
      {!!loadError && <ListState kind="error" things="MCP profiles and connections" error={loadError} onRetry={refresh} />}
      <fieldset disabled={locked || loading} className="space-y-3">
        <ChoiceGroup label="Operator-approved profile" value={profile?.id ?? ""} options={(catalog?.profiles ?? []).map((p) => ({ value: p.id, label: p.label || p.id }))}
          onChange={(id) => { onChange(null); setProfile(catalog?.profiles.find((p) => p.id === id) ?? null); setSlot(""); setBearer(""); setNotice(""); }} />
        {profile && <div className="text-sm space-y-1">
          <p>Endpoint origin: {origin ?? "Not disclosed. Connection setup is unavailable until the operator discloses the trusted HTTPS origin."} This is operator-approved metadata; it does not verify remote identity.</p>
          <p>Authentication: {profile.auth === "bearer" ? "Preissued bearer sent to this approved server" : "Anonymous"}.</p>
          <p>Approved tools: {profile.approved_tools.join(", ") || "None declared"}. Operator collection-copy approval: {profile.allow_collection_copy ? "yes" : "no"}.</p>
        </div>}
        {profile && <ChoiceGroup label="Your connections for this profile" value={slot}
          options={choices.map((c) => ({ value: c.slot, label: `${c.label || c.slot} (${c.state})` }))}
          onChange={(value) => { onChange(null); setSlot(value); setBearer(""); setDisconnectConfirmed(false); setNotice(""); }} />}
        {selected && <Button type="secondary" htmlType="button" disabled={!origin || selected.state !== "configured"} onClick={() => void useConnection(selected, "explicit")}>Discover capabilities</Button>}
        {profile?.auth === "bearer" && origin && <PasswordField label="Preissued upstream bearer" value={bearer} autoComplete="off"
          onChange={(value) => { onChange(null); setBearer(value); }} />}
        {profile && <TextField label="Connection label (optional; does not verify identity)" value={connectionLabel} onChange={setConnectionLabel} />}
        {profile && <Button type="secondary" htmlType="button" disabled={!origin || !catalog?.self_service_enabled || connectionLabel.length > 256 || (profile.auth === "bearer" && !bearer)} onClick={() => void connect("explicit")}>Connect a new account</Button>}
        {catalog && !catalog.self_service_enabled && <p>New connection registration is disabled by the operator.</p>}
      </fieldset>
      <div className="flex flex-wrap gap-2">
        <Button type="secondary" htmlType="button" disabled={locked || loading} onClick={refresh}>Refresh profiles and accounts</Button>
        {catalog?.next_cursor && <Button type="secondary" htmlType="button" disabled={locked || loading || catalog.next_cursor === profileCursor}
          onClick={() => { onChange(null); setProfile(null); setSlot(""); setBearer(""); setProfileCursor(catalog.next_cursor); }}>More profiles</Button>}
        {accounts?.next_cursor && <Button type="secondary" htmlType="button" disabled={locked || loading || accounts.next_cursor === connectionCursor}
          onClick={() => { onChange(null); setSlot(""); setConnectionCursor(accounts.next_cursor); }}>More connections</Button>}
      </div>
      {(pending || disconnectUncertain) && <><Alert state="warning" text={copy.uncertain} /><Button type="secondary" htmlType="button" disabled={request.busy || disabled} onClick={() => void reconcile()}>Inspect connection result</Button></>}
      {selected && <fieldset disabled={disabled || request.busy || pending !== null || disconnectUncertain} className="space-y-2">
        <Checkbox label="Disconnect this connection and stop all registrations bound to it. I will revoke the upstream credential manually." checked={disconnectConfirmed} onCheckedChange={(v) => setDisconnectConfirmed(v === true)} />
        <Button type="secondary" htmlType="button" disabled={!disconnectConfirmed} onClick={() => void disconnect("explicit")}>Disconnect connection</Button>
      </fieldset>}
      {!!request.error && <Alert role="alert" state="danger" text="The connection request did not return a usable result. Refresh a read, or inspect an uncertain mutation. No credential will be resubmitted." />}
      {discovered?.key === scope && <div className="space-y-1 text-sm">
        <p>Server discovery: resources {discovered.value.resources ? "yes" : "no"}; approved read mapping {discovered.value.mapped_read ? "yes" : "no"}; approved search mapping {discovered.value.mapped_search ? "yes" : "no"}. These capabilities do not define or enumerate your selection.</p>
        <ul aria-label="Discovery limitations" className="list-disc pl-5">{discovered.value.limitations.map((line, index) => <li key={`${index}:${line}`}>{line}</li>)}</ul>
      </div>}
      {notice && <p role="status" className="text-sm">{notice}</p>}
    </section>
  );
}

function disclosedOrigin(value: string | undefined): string | null {
  if (!value || /[^\x21-\x7e]/.test(value)) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && url.origin === value ? value : null;
  } catch { return null; }
}
