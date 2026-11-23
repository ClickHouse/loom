/**
 * The document sources a namespace's admins register for Loom to pull its documents from
 * (`knowledge.source.*`), as the repository card and the Library show them: the wire
 * shapes, the last sync in plain words, and the add form. Pure: no I/O, no React.
 */

/** A source's sync state: a request waiting for the worker, and what its last turn did. */
export interface KnowledgeSourceSync {
  /** When `sync_now` asked for a pass the worker has not run yet; "" = none waiting. */
  requested_at: string;
  /** When the worker last finished a turn on the source; "" = never. */
  last_synced_at: string;
  /** ok · failed · rate_limited · credential_invalid · provider_removed; "" = never run. */
  status: string;
  documents_seen: number;
  changed: number;
  removed: number;
  /** Documents not ingested. */
  unconverted: number;
  /** Why, as reason -> documents. */
  unconverted_reasons: Record<string, number>;
  /** Documents not written because another writer's document holds their address. */
  skipped: number;
  /** Why the turn failed, or what a turn that did not fail noted; "" when neither. */
  error: string;
}

export interface KnowledgeSource {
  name: string;
  kind: string;
  /** The ids and filters the source reads, in the form that names ids per kind; {} when
   *  an item has no place in it. Never a credential. */
  config: Record<string, unknown>;
  /** What the source reads, one object per item with its `type`; absent from an older
   *  server. */
  items?: Record<string, unknown>[];
  /** The file kinds a gdrive source reads; absent from an older server. */
  include?: string[];
  created_by: string;
  created_at: string;
  sync: KnowledgeSourceSync;
}

/** A document a source wrote, as knowledge.source.inspect lists it. */
export interface KnowledgeSourceDocument {
  source_uri: string;
  title: string;
  source_revision: string;
  ingested_at: string;
}

/** knowledge.source.inspect: the source and the documents it wrote. */
export interface KnowledgeSourceDetail extends KnowledgeSource {
  documents: KnowledgeSourceDocument[];
}

export interface KnowledgeSourceList {
  sources: KnowledgeSource[];
  /** Whether this deployment runs the sync worker. */
  enabled: boolean;
  /** False where add would refuse the namespace; absent from an older server. */
  accepts_sources?: boolean;
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
const KIND_LABEL: Record<string, string> = {
  recording: "meeting transcripts",
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? `Unsupported source (${kind})`;
}

/** The credential field's label for a kind, in the add and rotate forms. */
const CREDENTIAL_LABEL: Record<string, string> = {
  recording: "Read-only credential (JSON; stored encrypted, never shown again)",
};

export function credentialLabel(kind: string): string {
  return CREDENTIAL_LABEL[kind] ?? "Credential (stored encrypted, never shown again)";
}

export type SyncTone = "ok" | "warn" | "danger" | "muted";

export interface SyncBadge {
  text: string;
  tone: SyncTone;
  /** The badge's tooltip: what the state means. */
  tip: string;
}

/** A note that says the turn stopped short: the server puts these marks first, and only
 *  they start with `listing:` on a turn that did not fail. */
const CUT_SHORT = ["pass bound reached", "listing:"];

/** The last sync as one badge. A turn that ended `ok` with an `error` left a note: cut
 *  short (the pass bound, an incomplete listing) it reads "Incomplete"; any other note (an
 *  item or a file it could not read) reads "Synced with a note". The next pass goes on. */
export function syncBadge(sync: KnowledgeSourceSync): SyncBadge {
  switch (sync.status) {
    case "":
      return { text: "Never synced", tone: "muted", tip: "Loom has not synced this source yet." };
    case "ok":
      if (!sync.error) return { text: "Synced", tone: "ok", tip: "The last sync read every changed document." };
      if (CUT_SHORT.some((marker) => sync.error.startsWith(marker))) {
        return {
          text: "Incomplete",
          tone: "warn",
          tip: `The last sync stopped early (${sync.error}); the next one goes on.`,
        };
      }
      return { text: "Synced with a note", tone: "warn", tip: `The last sync noted: ${sync.error}.` };
    case "provider_removed":
      return {
        text: "Provider unavailable",
        tone: "warn",
        tip: "This provider is unavailable. Retained bytes and history do not grant current access.",
      };
    case "rate_limited":
      return { text: "Rate limited", tone: "warn", tip: "The source limited the last sync; the next one retries." };
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
        tip: sync.error ? `The last sync failed: ${sync.error}.` : "The last sync failed.",
      };
    default:
      return { text: sync.status, tone: "warn", tip: sync.error || sync.status };
  }
}

/** "40 documents listed · 3 written · 1 retired · 2 not converted · 1 skipped"; "" before a
 *  first turn. Every count is information: nothing here waits for a person. */
export function syncCounts(sync: KnowledgeSourceSync): string {
  if (!sync.status) return "";
  const parts = [`${sync.documents_seen} ${sync.documents_seen === 1 ? "document" : "documents"} listed`];
  parts.push(`${sync.changed} written`);
  if (sync.removed) parts.push(`${sync.removed} retired`);
  if (sync.unconverted) parts.push(`${sync.unconverted} not converted`);
  if (sync.skipped) parts.push(`${sync.skipped} skipped`);
  return parts.join(" · ");
}

const REASON_LABEL: Record<string, string> = {
  no_address: "no https link",
  no_extractor: "file type not readable",
  too_large: "over the size limit",
  extractor_unavailable: "file reading unavailable",
};

/** Why documents were not converted, most first: "file type not readable: 2; http_404: 1".
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

/** Recording setup is separate from generic MCP document sources. */
export const KIND_FORM = {
  recording: {
    about: "Zoom cloud recording transcripts. Paste a server-to-server app's JSON credential (account_id, client_id, client_secret) with read-only scopes; Loom checks it and every meeting id before anything is stored.",
    reads: "The credential reads the meetings",
    noCredential: "Paste the read-only credential.",
  },
};

export interface SourceForm {
  name: string;
  kind: "recording";
  meetings: string;
  credential: string;
}

export function emptySourceForm(): SourceForm {
  return { name: "", kind: "recording", meetings: "", credential: "" };
}

const NAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;
const MEETING_ID_RE = /^[0-9]{9,12}$/;
const MAX_IDS = 20;
const LINES = /[\r\n,]+/;

function pieces(text: string, separators: RegExp, normal: (piece: string) => string): string[] {
  const out: string[] = [];
  for (const piece of text.split(separators)) {
    const value = normal(piece);
    if (value && !out.includes(value)) out.push(value);
  }
  return out;
}

/** Recording meeting IDs may include spaces between digit groups. */
export function meetingIds(text: string): string[] {
  return pieces(text, LINES, (p) => {
    const digits = p.replace(/\s+/g, "");
    return /^[0-9]{1,12}$/.test(digits) ? digits : p.trim();
  });
}

/** Meeting ids' problems. Spaced digits left whole are too long for one id; 18 or more
 *  of them (two ids of at least 9) are two ids on one line. */
function zoomProblems(ids: string[]): string[] {
  const joined = ids.filter((id) => /\s/.test(id) && /^[0-9\s]+$/.test(id) && id.replace(/\s/g, "").length >= 18);
  const rest = ids.filter((id) => !joined.includes(id));
  // Only joined runs typed: there are ids, they are just on one line.
  const problems =
    joined.length > 0 && rest.length === 0
      ? []
      : idProblems(rest, MEETING_ID_RE, "meeting id", "a meeting id (9-12 digits)");
  if (joined.length > 0) problems.push(`Put one meeting id per line, or comma-separated: ${joined.join("; ")}.`);
  return problems;
}

export function normalisedName(name: string): string {
  return name.trim().toLowerCase();
}

/** A list of ids: at least one, at most MAX_IDS, each matching `re`. */
function idProblems(ids: string[], re: RegExp, what: string, notOne: string): string[] {
  if (ids.length === 0) return [`Add at least one ${what}.`];
  const problems = ids.length > MAX_IDS ? [`At most ${MAX_IDS} ${what}s.`] : [];
  const bad = ids.filter((id) => !re.test(id));
  if (bad.length > 0) problems.push(`Not ${notOne}: ${bad.join(", ")}.`);
  return problems;
}

export function sourceFormProblems(f: SourceForm): string[] {
  const problems: string[] = [];
  if (!NAME_RE.test(normalisedName(f.name)))
    problems.push("Name: 2-32 lower-case letters, digits, - or _, starting with a letter.");
  problems.push(...zoomProblems(meetingIds(f.meetings)));
  if (!f.credential.trim()) problems.push(KIND_FORM[f.kind].noCredential);
  return problems;
}

/** Show validation once a recording field has been touched. */
export function sourceFormTouched(f: SourceForm): boolean {
  return [f.name, f.meetings, f.credential].some((v) => v !== "");
}

export function sourceAddBody(org: string, namespace: string, f: SourceForm, dryRun: boolean) {
  return {
    org,
    namespace,
    name: normalisedName(f.name),
    kind: "recording",
    config: { provider: "zoom", ids: meetingIds(f.meetings) },
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
