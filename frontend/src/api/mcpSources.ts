import { post } from "../api";
import type { DashboardRequestIntent } from "../lib/dashboardTransport";

export type MCPMode = "live" | "snapshot" | "selected_refresh";
export type MCPAudience = "owner_only" | "collection_copy";
export interface MCPProfile {
  id: string;
  label: string;
  auth: "none" | "bearer";
  endpoint_origin: string;
  profile_sha256: string;
  approved_tools: string[];
  allow_collection_copy: boolean;
}
export interface MCPConnection {
  slot: string;
  generation: string;
  state: "configured" | "stopped" | "unavailable" | "approval_drift";
  profile_id: string | null;
  profile_sha256: string | null;
  label: string | null;
  compatibility: "unchecked";
  remote_identity_verified: false;
}
export interface MCPDiscovery {
  slot: string;
  generation: string;
  resources: boolean;
  mapped_read: boolean;
  mapped_search: boolean;
  supported_reads: boolean;
  limitations: string[];
  remote_identity_verified: false;
}
export interface MCPBrowse {
  slot: string;
  generation: string;
  mode: "resources" | "search";
  supported: boolean;
  resources: boolean | null;
  mapped_read: boolean;
  mapped_search: boolean;
  capabilities_checked: boolean;
  items: { id: string; title: string; uri: string }[];
  complete: boolean;
  truncated: boolean;
  reason: string | null;
  limitations: string[];
  manual_resource_ids_supported: true;
  remote_identity_verified: false;
}
export interface MCPLiveRead {
  source_id: string;
  resource_id: string;
  uri: string;
  title: string;
  text: string;
  mime_type: string;
  resource_identity_verified: boolean;
  remote_identity_verified: false;
  upstream_acl_verified: false;
  limitations: string[];
}
export interface MCPAccessRule {
  access: "owner_only" | "collection_wide" | "unavailable";
  reason: string;
  stores_copy: boolean;
  future_items_included: false;
  upstream_permissions: "unsupported";
  reader_list_complete: false;
  collection_readers_receive_copy: boolean;
  inherited_grants_apply: true;
  membership_changes_affect_copy: boolean;
  publication_confirmation_required: boolean;
  explanation: string;
}
export interface MCPPreview {
  namespace: string;
  mode: MCPMode;
  rule: MCPAccessRule;
  connection_state: MCPConnection["state"];
  compatibility: "unchecked";
  permission_evidence: "unsupported";
  refresh_state: "not_applicable" | "enabled" | "disabled";
  selected_count: number;
  checked_at: string;
  read_evidence_stale_seconds: number;
  preview_grants_authority: false;
  limitations: string[];
}
export interface MCPSourceSummary {
  name: string;
  source_id: string;
  slot: string;
  namespace: string;
  state: "registered" | "paused" | "retired" | "unsupported";
  mode: MCPMode | null;
  reader_policy: MCPAudience | null;
  scope_revision: number | null;
  label: string | null;
  selected_count: number | null;
  sync_enabled: boolean;
  read_evidence_stale_seconds: number;
  refresh_state: string;
  compatibility: "unchecked";
  upstream_permissions: "unsupported";
  complete_enumeration: false;
  upstream_deletion_tracking: false;
  future_items_included: false;
  current_access_attested: false;
  local_authority: "current" | "unavailable" | "unchecked";
  pending_erasure: boolean;
  rule: MCPAccessRule | null;
}
export interface MCPSource extends MCPSourceSummary { resource_ids: string[] }
export interface MCPSourceResult {
  source: MCPSource;
  copies: "retained" | "erasure_requested";
  refresh_requested: boolean;
}
export interface MCPSelection {
  org: string;
  namespace: string;
  slot: string;
  resource_ids: string[];
  mode: MCPMode;
  reader_policy: MCPAudience;
}
export interface MCPSourceKey { org: string; namespace: string; name: string }

// Use the dashboard transport unchanged. Protected-result/session refusals remain refusals.
function call<T>(op: string, body: object, signal?: AbortSignal, intent: DashboardRequestIntent = "automatic"): Promise<T> {
  return post<T>(`/v1/knowledge.${op}`, body, signal, intent);
}
export function mcpProfiles(org: string, cursor: string | null, signal?: AbortSignal) {
  return call<{ profiles: MCPProfile[]; next_cursor: string | null; self_service_enabled: boolean }>(
    "source.mcp_profiles", { org, cursor, limit: 100 }, signal,
  );
}
export function mcpConnections(org: string, cursor: string | null, signal?: AbortSignal) {
  return call<{ connections: MCPConnection[]; next_cursor: string | null }>(
    "connection.mcp_list", { org, cursor, limit: 100 }, signal,
  );
}
export function mcpConnect(input: { org: string; profile_id: string; request_id: string; label?: string; bearer?: string }, signal?: AbortSignal, intent: DashboardRequestIntent = "automatic") {
  return call<{ connection: MCPConnection }>("connection.mcp_connect", {
    org: input.org, profile_id: input.profile_id, request_id: input.request_id,
    ...(input.label === undefined ? {} : { label: input.label }),
    ...(input.bearer === undefined ? {} : { bearer: input.bearer }),
  }, signal, intent);
}
export function mcpInspectConnection(org: string, key: { slot?: string; request_id?: string }, signal?: AbortSignal) {
  return call<{ found: boolean; slot: string; connection: MCPConnection | null; reconciliation: "absent" | "present" | "incomplete" }>(
    "connection.mcp_inspect", { org, slot: key.slot ?? "", request_id: key.request_id ?? "" }, signal,
  );
}
export function mcpDiscover(org: string, slot: string, signal?: AbortSignal, intent: DashboardRequestIntent = "automatic") {
  return call<MCPDiscovery>("connection.mcp_discover", { org, slot }, signal, intent);
}
export function mcpBrowse(input: { org: string; slot: string; mode: "resources" | "search"; query: string; limit: number }, signal?: AbortSignal) {
  return call<MCPBrowse>("connection.mcp_browse", { org: input.org, slot: input.slot,
    mode: input.mode, query: input.mode === "search" ? input.query : "", limit: input.limit }, signal);
}
export function mcpRead(input: MCPSourceKey & { resource_id: string }, signal?: AbortSignal) {
  return call<MCPLiveRead>("source.mcp_read", { ...sourceKey(input), resource_id: input.resource_id }, signal);
}
export function mcpDisconnect(org: string, slot: string, signal?: AbortSignal, intent: DashboardRequestIntent = "automatic") {
  return call<{ disconnected: boolean; upstream_revoked: false; upstream_action: "revoke_credential_upstream" }>(
    "connection.mcp_disconnect", { org, slot }, signal, intent,
  );
}
function selection(input: MCPSelection): MCPSelection {
  return { org: input.org, namespace: input.namespace, slot: input.slot, resource_ids: input.resource_ids,
    mode: input.mode, reader_policy: input.reader_policy };
}
export function mcpPreview(input: MCPSelection, signal?: AbortSignal) {
  return call<MCPPreview>("source.mcp_preview", selection(input), signal);
}
export function mcpAdd(input: MCPSelection & { request_id: string; label: string | null; confirm_copy_authority: boolean }, signal?: AbortSignal, intent: DashboardRequestIntent = "automatic") {
  return call<MCPSourceResult>("source.mcp_add", { ...selection(input), request_id: input.request_id,
    label: input.label, confirm_copy_authority: input.confirm_copy_authority }, signal, intent);
}
function sourceKey(input: MCPSourceKey): MCPSourceKey {
  return { org: input.org, namespace: input.namespace, name: input.name };
}
export function mcpList(org: string, namespace: string, cursor: string | null, signal?: AbortSignal) {
  return call<{ sources: MCPSourceSummary[]; next_cursor: string | null }>(
    "source.mcp_list", { org, namespace, cursor, limit: 20, include_retired: true }, signal,
  );
}
export function mcpInspect(input: { org: string; namespace: string } & ({ name: string } | { slot: string; request_id: string }), signal?: AbortSignal) {
  return call<{ found: boolean; source: MCPSource | null }>("source.mcp_inspect", {
    org: input.org, namespace: input.namespace,
    ...("name" in input ? { name: input.name } : { slot: input.slot, request_id: input.request_id }),
  }, signal);
}
export function mcpUpdate(input: MCPSourceKey & { if_scope_revision: number; resource_ids: string[]; mode: MCPMode; label: string | null; confirm_copy_authority: boolean }, signal?: AbortSignal, intent: DashboardRequestIntent = "automatic") {
  return call<MCPSourceResult>("source.mcp_update", { ...sourceKey(input), if_scope_revision: input.if_scope_revision,
    resource_ids: input.resource_ids, mode: input.mode, label: input.label, confirm_copy_authority: input.confirm_copy_authority }, signal, intent);
}
export function mcpControl(input: MCPSourceKey, action: "pause" | "resume" | "refresh", signal?: AbortSignal, intent: DashboardRequestIntent = "automatic") {
  return call<MCPSourceResult>(`source.mcp_${action}`, sourceKey(input), signal, intent);
}
export function mcpRemove(input: MCPSourceKey & { confirm: string; delete_documents: boolean }, signal?: AbortSignal, intent: DashboardRequestIntent = "automatic") {
  return call<MCPSourceResult>("source.mcp_remove", { ...sourceKey(input), confirm: input.confirm,
    delete_documents: input.delete_documents }, signal, intent);
}
