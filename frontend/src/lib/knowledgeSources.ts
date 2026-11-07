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
  /** Documents not ingested. */
  unconverted: number;
  /** Why, as reason -> documents. */
  unconverted_reasons: Record<string, number>;
  /** Documents not written because another writer's document holds their address. */
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
const KIND_LABEL: Record<string, string> = {
  gdrive: "shared folder",
  linear: "project documents",
  notion: "pages",
  recording: "meeting transcripts",
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind;
}

/** The credential field's label for a kind, in the add and rotate forms. */
const CREDENTIAL_LABEL: Record<string, string> = {
  gdrive: "Service account key (JSON; stored encrypted, never shown again)",
  linear: "Read-only API key (stored encrypted, never shown again)",
  notion: "Integration token (stored encrypted, never shown again)",
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
        : { text: "Synced", tone: "ok", tip: "The last turn read every changed document." };
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
  no_extractor: "no extractor for the format",
  too_large: "over the size limit",
  extractor_unavailable: "extractor unavailable",
};

/** Why documents were not converted, most first: "no extractor for the format: 2; http_404: 1".
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

/** The kinds the add form serves, in the order the form offers them. */
const SOURCE_KINDS = ["gdrive", "linear", "notion", "recording"] as const;
type SourceKind = (typeof SOURCE_KINDS)[number];

/** What the form says about each kind: the dialog's description, what a passed check
 *  read, and what to ask for when no credential is typed. */
export const KIND_FORM: Record<SourceKind, { about: string; reads: string; noCredential: string }> = {
  gdrive: {
    about:
      "A shared folder of documents, presentations, PDFs and Word documents. Share it read-only with a service account and paste the account's key; Loom checks the key and the folders before anything is stored.",
    reads: "The key reads the folders",
    noCredential: "Paste the service account's key.",
  },
  linear: {
    about:
      "The documents of a project tracker's projects. Paste a read-only API key of an account that sees the projects; Loom checks the key and the projects before anything is stored.",
    reads: "The key reads the projects",
    noCredential: "Paste the read-only API key.",
  },
  notion: {
    about:
      "A page store's pages and databases. Share them with an integration that has read content access and paste its token; Loom checks the token and every page and database before anything is stored.",
    reads: "The token reads the pages and databases",
    noCredential: "Paste the integration token.",
  },
  recording: {
    about:
      "The transcripts of a meeting store's recordings. Paste a credential that grants read-only scopes only; Loom checks it and every id before anything is stored.",
    reads: "The credential reads the meetings",
    noCredential: "Paste the read-only credential.",
  },
};

/** The kind choice, in the card's words for each kind: "Shared folder", "Pages". */
export const KIND_OPTIONS = SOURCE_KINDS.map((k) => {
  const label = kindLabel(k);
  return { value: k, label: label.charAt(0).toUpperCase() + label.slice(1) };
});

/** The file kinds a shared folder's `include` takes, with the words the form shows. */
export const INCLUDE_OPTIONS = [
  { value: "docs", label: "Documents" },
  { value: "slides", label: "Presentations" },
  { value: "pdf", label: "PDF files" },
  { value: "pptx", label: ".pptx decks" },
  { value: "docx", label: "Word documents" },
] as const;

/** A meeting store's providers, with the credential each takes. */
export const RECORDING_PROVIDERS = [
  { value: "meet", label: "meet", hint: "An authorized_user JSON file with the meetings.space.readonly scope only" },
  {
    value: "zoom",
    label: "zoom",
    hint: "A server-to-server app's JSON (account_id, client_id, client_secret) with read-only scopes",
  },
] as const;
type RecordingProvider = (typeof RECORDING_PROVIDERS)[number]["value"];

/** Each kind's id fields live side by side, so switching the kind keeps them; only the
 *  chosen kind's are sent, and the credential is cleared. */
export interface SourceForm {
  name: string;
  kind: SourceKind;
  /** gdrive: folder ids or folder links, one per line or comma-separated. */
  folders: string;
  include: string[];
  /** linear: project ids (UUIDs). */
  projects: string;
  /** notion: page ids and database ids, or their links. */
  pages: string;
  databases: string;
  /** recording: the provider, and its space ids (meet) or meeting ids (zoom). */
  provider: RecordingProvider;
  meetings: string;
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
  return {
    name: "",
    kind: "gdrive",
    folders: "",
    // .docx is opt-in, as in the server's default include.
    include: INCLUDE_OPTIONS.map((o) => o.value).filter((v) => v !== "docx"),
    projects: "",
    pages: "",
    databases: "",
    provider: "meet",
    meetings: "",
    credential: "",
  };
}

// The server's rules (server/knowledge/domain/sources.py), checked here so Check stays
// off until the server would read the config; the server checks them again.
const NAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;
const FOLDER_ID_RE = /^[A-Za-z0-9_-]{10,128}$/;
const PROJECT_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PAGE_ID_RE = /^[0-9a-f]{32}$/;
const MEETING_ID_RE: Record<RecordingProvider, RegExp> = {
  meet: /^[A-Za-z0-9_-]{1,128}$/,
  zoom: /^[0-9]{9,12}$/,
};
// Each kind takes 1 to 20 ids; a page store's pages and databases count together.
const MAX_IDS = 20;

// What separates the ids typed into a field: any space, comma or line break; a meeting
// id is shown with spaces inside it, so its field splits on commas and lines only.
const ANY_SPACE = /[\s,]+/;
const LINES = /[\r\n,]+/;

/** The pieces of what was typed, each normalised, empty ones and duplicates dropped. */
function pieces(text: string, separators: RegExp, normal: (piece: string) => string): string[] {
  const out: string[] = [];
  for (const piece of text.split(separators)) {
    const value = normal(piece);
    if (value && !out.includes(value)) out.push(value);
  }
  return out;
}

/** The folder ids in what was typed: a bare id, or the id after `/folders/` in a pasted
 *  folder link. Duplicates are dropped, the order kept. */
export function folderIds(text: string): string[] {
  return pieces(text, ANY_SPACE, (p) => /\/folders\/([^/?#]+)/.exec(p)?.[1] ?? p);
}

/** Project ids, lower-cased as the server stores them; any other piece (a pasted link)
 *  is kept as typed, so the form can name it. */
export function projectIds(text: string): string[] {
  return pieces(text, ANY_SPACE, (p) => (PROJECT_ID_RE.test(p.toLowerCase()) ? p.toLowerCase() : p));
}

/** A bare id (dashed or not), a peek link's `p=` (pages field), or the 32 hex digits ending
 *  a link's last path segment; a link in the wrong field, or anything else, is kept as
 *  typed so the form names it. Lower-case without dashes; the server stores it dashed. */
function pageStoreId(piece: string, inPages: boolean): string {
  if (!piece.includes("/")) return bareId(piece) || piece;
  const page = inPages ? /[?&]p=([^&#]*)/.exec(piece)?.[1] : undefined;
  if (page !== undefined) return bareId(page) || piece;
  if (inPages ? isDatabaseLink(piece) : isPageLink(piece)) return piece;
  // The id is the whole segment, or what follows the title's last `-`.
  const segment = (piece.split(/[?#]/)[0] ?? "").replace(/\/+$/, "").split("/").pop() ?? "";
  return /(?:^|-)([0-9a-f]{32})$/.exec(segment.toLowerCase())?.[1] ?? piece;
}

/** A page store id with or without dashes as its 32 lower-case hex digits, or "". */
function bareId(text: string): string {
  const hexes = text.toLowerCase().replace(/-/g, "");
  return PAGE_ID_RE.test(hexes) ? hexes : "";
}

/** A link to a database's view: a `v=` and no `p=`. */
function isDatabaseLink(piece: string): boolean {
  return /[?&]v=/.test(piece) && !/[?&]p=/.test(piece);
}

/** A peek link to a page from its parent page: a `p=` and no `v=`. */
function isPageLink(piece: string): boolean {
  return /[?&]p=/.test(piece) && !/[?&]v=/.test(piece);
}

/** Page ids; a peek link gives the page it opens. */
export function pageIds(text: string): string[] {
  return pieces(text, ANY_SPACE, (p) => pageStoreId(p, true));
}

/** Database ids; a peek link gives its database, never the page it opens. */
export function databaseIds(text: string): string[] {
  return pieces(text, ANY_SPACE, (p) => pageStoreId(p, false));
}

/** Meeting space ids (a `spaces/` prefix dropped), or meeting ids with the spaces they
 *  are shown with dropped (`812 3456 7890`). Any other piece is kept as typed, so the
 *  form can name it. */
export function meetingIds(text: string, provider: RecordingProvider): string[] {
  if (provider === "meet") return pieces(text, ANY_SPACE, (p) => p.replace(/^spaces\//, ""));
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
      : idProblems(rest, MEETING_ID_RE.zoom, "meeting id", "a meeting id (9-12 digits)");
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

function configProblems(f: SourceForm): string[] {
  switch (f.kind) {
    case "gdrive": {
      const problems = idProblems(folderIds(f.folders), FOLDER_ID_RE, "folder", "a folder id");
      if (f.include.length === 0) problems.push("Pick at least one file kind.");
      return problems;
    }
    case "linear":
      return idProblems(projectIds(f.projects), PROJECT_ID_RE, "project", "a project id (a UUID)");
    case "notion": {
      const pages = pageIds(f.pages);
      const databases = databaseIds(f.databases);
      const all = [...pages, ...databases];
      if (all.length === 0) return ["Add at least one page or database."];
      const problems = all.length > MAX_IDS ? [`At most ${MAX_IDS} pages and databases in all.`] : [];
      const linked = pages.filter(isDatabaseLink);
      if (linked.length > 0) problems.push(`Paste a database link under Databases: ${linked.join(", ")}.`);
      const pageLinks = databases.filter(isPageLink);
      if (pageLinks.length > 0) problems.push(`Paste a page link under Pages: ${pageLinks.join(", ")}.`);
      const bad = all.filter((id) => !PAGE_ID_RE.test(id) && !linked.includes(id) && !pageLinks.includes(id));
      if (bad.length > 0) problems.push(`Not an id (32 hex digits): ${bad.join(", ")}.`);
      return problems;
    }
    case "recording":
      return f.provider === "meet"
        ? idProblems(meetingIds(f.meetings, "meet"), MEETING_ID_RE.meet, "space id", "a space id")
        : zoomProblems(meetingIds(f.meetings, "zoom"));
  }
}

/** What keeps the form from being checked, one line each; [] when it can be. */
export function sourceFormProblems(f: SourceForm): string[] {
  const problems: string[] = [];
  if (!NAME_RE.test(normalisedName(f.name)))
    problems.push("Name: 2-32 lower-case letters, digits, - or _, starting with a letter.");
  problems.push(...configProblems(f));
  if (!f.credential.trim()) problems.push(KIND_FORM[f.kind].noCredential);
  return problems;
}

/** The chosen kind's config, in the keys the server reads for it. */
function sourceConfig(f: SourceForm): Record<string, unknown> {
  switch (f.kind) {
    case "gdrive":
      return { folder_ids: folderIds(f.folders), include: [...f.include] };
    case "linear":
      return { project_ids: projectIds(f.projects) };
    case "notion":
      return { page_ids: pageIds(f.pages), database_ids: databaseIds(f.databases) };
    case "recording":
      return { provider: f.provider, ids: meetingIds(f.meetings, f.provider) };
  }
}

/** Whether anything but the kind was typed, so the problems list can wait. */
export function sourceFormTouched(f: SourceForm): boolean {
  return [f.name, f.folders, f.projects, f.pages, f.databases, f.meetings, f.credential].some((v) => v !== "");
}

export function sourceAddBody(org: string, namespace: string, f: SourceForm, dryRun: boolean) {
  return {
    org,
    namespace,
    name: normalisedName(f.name),
    kind: f.kind,
    config: sourceConfig(f),
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
