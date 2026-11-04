/**
 * The document sources a repository's maintainers register for Loom to pull its documents
 * from (`knowledge.source.*`), as the repository card shows them: the wire shapes, the
 * last sync in plain words, and the add form. Pure: no I/O, no React.
 */

/** A source's sync state: a request waiting for the worker, and what its last turn did. */
export interface KnowledgeSourceSync {
  /** When `sync_now` asked for a pass the worker has not run yet; "" = none waiting. */
  requested_at: string;
  /** When the worker last finished a turn on the source; "" = never. */
  last_synced_at: string;
  /** ok · failed · rate_limited · credential_invalid; "" = never run. */
  status: string;
  documents_seen: number;
  changed: number;
  removed: number;
  /** Files not ingested, and why, as reason -> files. */
  unconverted: number;
  unconverted_reasons: Record<string, number>;
  /** Files not written because another writer's document holds their address. */
  skipped: number;
  /** Why the turn failed, or why a turn that did not fail is incomplete; "" when neither. */
  error: string;
}

export interface KnowledgeSource {
  name: string;
  kind: string;
  /** The ids and filters the source reads; never a credential. */
  config: Record<string, unknown>;
  created_by: string;
  created_at: string;
  sync: KnowledgeSourceSync;
}

export interface KnowledgeSourceList {
  sources: KnowledgeSource[];
  /** Whether this deployment runs the sync worker. */
  enabled: boolean;
}

export interface KnowledgeSourceProbe {
  documents: number;
  /** More than `documents` match: the count is a lower bound. */
  more: boolean;
  sample: string[];
}

export interface KnowledgeSourceAddResult {
  name: string;
  kind: string;
  namespace: string;
  config: Record<string, unknown>;
  probe: KnowledgeSourceProbe;
  dry_run: boolean;
}

export interface KnowledgeSourceCheckResult extends KnowledgeSourceProbe {
  name: string;
  reachable: boolean;
  summary: string;
  checked_at: string;
}

export interface KnowledgeSourceRotateResult {
  name: string;
  probe: KnowledgeSourceProbe;
  rotated_at: string;
}

export interface KnowledgeSourceRemoveResult {
  name: string;
  /** Live documents the recorded delete request covers; 0 when they are kept. */
  documents_pending: number;
}

export interface KnowledgeSourceSyncNowResult {
  name: string;
  enabled: boolean;
}

export const SYNC_OFF = "Sync is off on this deployment: sources are stored and checked, but nothing pulls from them.";

/** What each kind reads, in the words the card uses. */
const KIND_LABEL: Record<string, string> = { gdrive: "shared folder" };

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind;
}

export type SyncTone = "ok" | "warn" | "danger" | "muted";

export interface SyncBadge {
  text: string;
  tone: SyncTone;
  /** The badge's tooltip: what the state means. */
  tip: string;
}

/** The last sync as one badge. A turn that ended `ok` with an `error` was cut short (the
 *  pass bound, an incomplete listing): it reads "Incomplete", and the next pass goes on. */
export function syncBadge(sync: KnowledgeSourceSync): SyncBadge {
  switch (sync.status) {
    case "":
      return { text: "Never synced", tone: "muted", tip: "The sync worker has not run a turn on this source yet." };
    case "ok":
      return sync.error
        ? {
            text: "Incomplete",
            tone: "warn",
            tip: `The last turn stopped early (${sync.error}); the next one goes on.`,
          }
        : { text: "Synced", tone: "ok", tip: "The last turn read every changed file." };
    case "rate_limited":
      return { text: "Rate limited", tone: "warn", tip: "The source limited the last turn; the next one retries." };
    case "credential_invalid":
      return {
        text: "Credential refused",
        tone: "danger",
        tip: "The stored credential no longer works: rotate it.",
      };
    case "failed":
      return {
        text: "Sync failed",
        tone: "danger",
        tip: sync.error ? `The last turn failed: ${sync.error}.` : "The last turn failed.",
      };
    default:
      return { text: sync.status, tone: "warn", tip: sync.error || sync.status };
  }
}

/** "40 files listed · 3 written · 1 retired · 2 not converted · 1 skipped"; "" before a
 *  first turn. Every count is information: nothing here waits for a person. */
export function syncCounts(sync: KnowledgeSourceSync): string {
  if (!sync.status) return "";
  const parts = [`${sync.documents_seen} ${sync.documents_seen === 1 ? "file" : "files"} listed`];
  parts.push(`${sync.changed} written`);
  if (sync.removed) parts.push(`${sync.removed} retired`);
  if (sync.unconverted) parts.push(`${sync.unconverted} not converted`);
  if (sync.skipped) parts.push(`${sync.skipped} skipped`);
  return parts.join(" · ");
}

const REASON_LABEL: Record<string, string> = {
  no_address: "no https link",
  no_extractor: "no extractor for the format",
  too_large: "over the size limit",
  extractor_unavailable: "extractor unavailable",
};

/** Why files were not converted, most first: "no extractor for the format: 2; too_large…".
 *  A reason the card has no words for is shown as the server wrote it. */
export function unconvertedDetail(reasons: Record<string, number>): string {
  return Object.entries(reasons)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([reason, n]) => `${REASON_LABEL[reason] ?? reason}: ${n}`)
    .join("; ");
}

// ---------------------------------------------------------------------------
// The add form
// ---------------------------------------------------------------------------

/** The file kinds a shared folder's `include` takes, with the words the form shows. */
export const INCLUDE_OPTIONS = [
  { value: "docs", label: "Documents" },
  { value: "slides", label: "Presentations" },
  { value: "pdf", label: "PDF files" },
  { value: "pptx", label: ".pptx decks" },
] as const;

export interface SourceForm {
  name: string;
  kind: string;
  /** Folder ids or folder links, one per line or comma-separated. */
  folders: string;
  include: string[];
  /** Held in the dialog only: sent with add, never shown back. */
  credential: string;
}

/** `include` with `kind` ticked or unticked, always in INCLUDE_OPTIONS order, so a box
 *  unticked and ticked again gives the form the check passed. */
export function toggleInclude(include: readonly string[], kind: string): string[] {
  const next = include.includes(kind) ? include.filter((k) => k !== kind) : [...include, kind];
  return INCLUDE_OPTIONS.map((o) => o.value).filter((v) => next.includes(v));
}

export function emptySourceForm(): SourceForm {
  return { name: "", kind: "gdrive", folders: "", include: INCLUDE_OPTIONS.map((o) => o.value), credential: "" };
}

const NAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;
const FOLDER_ID_RE = /^[A-Za-z0-9_-]{10,128}$/;
const MAX_FOLDERS = 20;

/** The folder ids in what was typed: a bare id, or the id after `/folders/` in a pasted
 *  folder link. Duplicates are dropped, the order kept. */
export function folderIds(text: string): string[] {
  const ids: string[] = [];
  for (const piece of text.split(/[\s,]+/)) {
    if (!piece) continue;
    const id = /\/folders\/([^/?#]+)/.exec(piece)?.[1] ?? piece;
    if (!ids.includes(id)) ids.push(id);
  }
  return ids;
}

export function normalisedName(name: string): string {
  return name.trim().toLowerCase();
}

/** What keeps the form from being checked, one line each; [] when it can be. */
export function sourceFormProblems(f: SourceForm): string[] {
  const problems: string[] = [];
  if (!NAME_RE.test(normalisedName(f.name)))
    problems.push("Name: 2-32 lower-case letters, digits, - or _, starting with a letter.");
  const ids = folderIds(f.folders);
  if (ids.length === 0) problems.push("Add at least one folder.");
  else if (ids.length > MAX_FOLDERS) problems.push(`At most ${MAX_FOLDERS} folders.`);
  const bad = ids.filter((id) => !FOLDER_ID_RE.test(id));
  if (bad.length > 0) problems.push(`Not a folder id: ${bad.join(", ")}.`);
  if (f.include.length === 0) problems.push("Pick at least one file kind.");
  if (!f.credential.trim()) problems.push("Paste the service account's key.");
  return problems;
}

export function sourceAddBody(org: string, namespace: string, f: SourceForm, dryRun: boolean) {
  return {
    org,
    namespace,
    name: normalisedName(f.name),
    kind: f.kind,
    config: { folder_ids: folderIds(f.folders), include: [...f.include] },
    credential: f.credential.trim(),
    dry_run: dryRun,
  };
}

/** A fingerprint of everything the check read: Add stays off until the form it would
 *  send is the one that passed. */
export function sourceFormFingerprint(f: SourceForm): string {
  return JSON.stringify(sourceAddBody("", "", f, true));
}

/** The toast after Retire: whether the documents stay, how many go, and that the
 *  retirement waits for the sync worker when it is off. */
export function retiredMessage(name: string, deleteDocuments: boolean, pending: number, enabled: boolean): string {
  if (!deleteDocuments) return `Retired ${name}; its documents stay`;
  if (pending === 0) return `Retired ${name}; it has no documents left to retire`;
  const docs = pending === 1 ? "1 document is" : `${pending} documents are`;
  return enabled
    ? `Retired ${name}; its ${docs} retired over the next sync passes`
    : `Retired ${name}; its ${docs} retired once sync is on for this deployment`;
}

/** "40 documents" / "1000+ documents". */
export function probeCount(p: KnowledgeSourceProbe): string {
  return `${p.documents}${p.more ? "+" : ""} ${p.documents === 1 && !p.more ? "document" : "documents"}`;
}
