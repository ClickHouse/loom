import type { SourceDestinationCopy } from "./SourceDestinationStep";

// Source-local copy is the catalog handoff boundary; no unresolved catalog keys are rendered.
export const mcpSourceCopy = {
  title: "MCP document sources",
  transport: "Connect to an operator-approved remote HTTPS MCP server using Streamable HTTP. Supported authentication is anonymous or a preissued bearer. OAuth and local stdio are not supported.",
  selection: "Enter explicit opaque resource IDs, one per line. IDs are sent exactly as entered; links are not normalized. Folder traversal, search-based scope rules, future-item inclusion and complete enumeration are not supported.",
  privacy: "Live lookup does not import documents. Protected source content requires current server authorization and fresh read evidence for both audiences. Snapshot read access expires without manual refresh; expiry does not erase retained bytes or history. Copies and protected progress state follow server retention and erasure rules. Ordinary content logs, traces and caches shared across readers exclude private content. Privileged database logs and backups may retain values after erasure; deployment operators control their access and retention. Disconnecting does not erase copies or revoke the upstream credential. This is not a zero-retention service.",
  audience: "A private source stays owner-only even in a team collection. Collection copies use current and future inherited collection grants. Those grants do not delegate your connection. Remote ACL and deletion synchronization are unsupported.",
  authority: "I have authority to publish this selection to current and future readers of this collection, including inherited grants.",
  retained: "Retained bytes do not grant current access. Reconnecting or changing an operator profile requires a new registration.",
  uncertain: "The mutation result is uncertain. The draft and request ID are retained in this open flow. Inspect server state before taking another action; the mutation will not be sent again.",
  modes: [
    { value: "live", label: "Live lookup", hint: "Read on demand without importing documents." },
    { value: "snapshot", label: "Selected snapshot", hint: "Copy the fixed selection; later refresh is explicit." },
    { value: "selected_refresh", label: "Refresh selected items", hint: "Refresh only these IDs when the server enables refresh." },
  ],
} as const;

export const mcpDestinationCopy: SourceDestinationCopy = {
  title: "Destination", description: "Choose or create an ordinary private or team collection. Review access before saving.",
  searchLabel: "Search this page", searchHint: "Search names and addresses on this page. Load more to read the next page.",
  pageScope: "this page", destinationsLabel: "Writable collections", loading: "Loading collections",
  loadError: "Collections could not be loaded.", things: "collections", empty: "No writable ordinary collections on this page.",
  previous: "Previous page", next: "Load more", refresh: "Refresh collections", create: "New collection",
  createHint: "Create at the top level or inside a writable collection on this page.",
  topLevelHint: "Top-level collection. Review its grants before publishing.",
  selectionHint: "The server checks effective access again when saving.",
  pagingError: "The next page repeated a cursor. Refresh the collection list.",
  pagingLimit: "More collections exist. This view has reached its page limit.",
  inheritanceHint: (parent) => `Inherits grants from ${parent}. Review effective access before publishing.`,
  created: (namespace) => `Collection confirmed: ${namespace}. Select it after reviewing the list.`,
  selected: (namespace) => `Selected destination: ${namespace}`, page: (number) => `Page ${number}`,
};
