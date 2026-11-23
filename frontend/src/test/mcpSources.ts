import type { MCPAccessRule, MCPConnection, MCPDiscovery, MCPPreview, MCPProfile, MCPSource, MCPSourceResult, MCPSourceSummary } from "../api/mcpSources";

export const mcpProfile: MCPProfile = {
  id: "approved", label: "Approved remote server", auth: "bearer", endpoint_origin: "https://source.example",
  profile_sha256: "profile-digest", approved_tools: ["read_selected"], allow_collection_copy: true,
};
export const mcpConnection: MCPConnection = {
  slot: "00000000-0000-4000-8000-000000000001", generation: "generation-a", state: "configured",
  profile_id: "approved", profile_sha256: "profile-digest", label: "My connection",
  compatibility: "unchecked", remote_identity_verified: false,
};
export const mcpDiscovery: MCPDiscovery = {
  slot: mcpConnection.slot, generation: mcpConnection.generation, resources: true, mapped_read: false,
  mapped_search: false, supported_reads: true, limitations: ["No complete enumeration"], remote_identity_verified: false,
};
export const mcpRule: MCPAccessRule = {
  access: "owner_only", reason: "private_source", stores_copy: true, future_items_included: false,
  upstream_permissions: "unsupported", reader_list_complete: false, collection_readers_receive_copy: false,
  inherited_grants_apply: true, membership_changes_affect_copy: false, publication_confirmation_required: false,
  explanation: "Only the signed-in source owner can read this source, including in a team collection.",
};
export const mcpPreviewValue: MCPPreview = {
  namespace: "col-team", mode: "snapshot", rule: mcpRule, connection_state: "configured",
  compatibility: "unchecked", permission_evidence: "unsupported", refresh_state: "not_applicable", selected_count: 1,
  checked_at: "2026-01-01T00:00:00Z", read_evidence_stale_seconds: 300, preview_grants_authority: false, limitations: ["Remote ACL synchronization is unsupported"],
};
export const mcpSummary: MCPSourceSummary = {
  name: "mcp-registration", source_id: "source-id", slot: mcpConnection.slot, namespace: "col-team",
  state: "registered", mode: "snapshot", reader_policy: "owner_only", scope_revision: 4, label: "Private documents",
  selected_count: 1, sync_enabled: false, read_evidence_stale_seconds: 300, refresh_state: "disabled", compatibility: "unchecked",
  upstream_permissions: "unsupported", complete_enumeration: false, upstream_deletion_tracking: false,
  future_items_included: false, current_access_attested: false, local_authority: "current", pending_erasure: false,
  rule: mcpRule,
};
export const mcpSource: MCPSource = { ...mcpSummary, resource_ids: ["opaque://A?x=%2F"] };
export const mcpResult: MCPSourceResult = { source: mcpSource, copies: "retained", refresh_requested: false };
