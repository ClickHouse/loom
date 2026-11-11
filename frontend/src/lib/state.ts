import { create } from "zustand";
import type { Identity, MyNamespaceEntry, WhoAmI } from "../api";
import { hasToken, isAuthDisabled, isProxyMode } from "../api";
import { sameIdentity } from "./boot";
import { NAMESPACE_NAME, isCollectionName } from "./namespace";
import { patchEntry, readPlace, rememberPlace, type InspectedOwner, type Place } from "./place";

export type View =
  | "memories"
  | "recall"
  | "profile"
  | "knowledge"
  | "sharing"
  | "sessions"
  | "runs"
  | "graph"
  | "assistant"
  | "help"
  | "browse"
  | "code"
  | "tasks"
  | "setup"
  | "admin:review_queue"
  | "admin:health"
  | "admin:guardrails"
  | "admin:policies"
  | "admin:activity"
  | "admin:usage"
  | "admin:agents"
  | "admin:users"
  | "admin:settings";

export const ALL_VIEWS: View[] = [
  "memories",
  "recall",
  "profile",
  "knowledge",
  "sharing",
  "sessions",
  "runs",
  "graph",
  "assistant",
  "help",
  "browse",
  "code",
  "tasks",
  "setup",
  "admin:review_queue",
  "admin:health",
  "admin:guardrails",
  "admin:policies",
  "admin:activity",
  "admin:usage",
  "admin:agents",
  "admin:users",
  "admin:settings",
];

/** A page's URL reads like its label. The view ids stay (they are the stable keys in
 *  code, tests and the ledger); a view without an entry here uses its id as its path. */
const CANONICAL_PATH: Partial<Record<View, string>> = {
  code: "repositories",
  knowledge: "library",
  sessions: "conversations",
  sharing: "flow",
  browse: "spaces",
  setup: "settings",
};

/** View → URL path segment, e.g. "admin:health" → "admin/health". */
export function viewToPath(v: View): string {
  return CANONICAL_PATH[v] ?? v.replace(":", "/");
}

/** URL path segment → View, or null when no view has that path. */
export function pathToView(p: string): View | null {
  return ALL_VIEWS.find((v) => viewToPath(v) === p) ?? null;
}

export function viewHref(v: View): string {
  return `/${viewToPath(v)}`;
}

/** Query-string keys a view owns. Graph links a walk with `?seed=`, the
 *  Repositories view a repo with `?repo=`; nobody else reads either, so a
 *  navigation carries a key only into the view that owns it — otherwise
 * `?seed=` rode along into every other view's URL. */
const VIEW_QUERY_KEYS: Partial<Record<View, readonly string[]>> = {
  // Patterns mode links a cell or an issue with
  // `?mode=patterns&component=…&severity=…&issue=N`; path mode links `?path=a->b`.
  graph: ["seed", "mode", "component", "severity", "issue", "path"],
  code: ["repo"],
  // The Tasks view links an inbox with `?inbox=` and an open task with `?task=` (the bell
  // deep-links a triage queue).
  tasks: ["inbox", "task"],
  // Settings carries its open tab (`?tab=memory|system`).
  setup: ["tab"],
  // The object open on a page rides its URL, so a deep link, a reload and Back reopen it:
  // the memory in the drawer, the document in the preview, the thread in the replay.
  memories: ["memory"],
  // The Library also carries the collection it shows (`?collection=`).
  knowledge: ["doc", "collection"],
  sessions: ["thread"],
  // The Help view's page of the docs (`?page=features/knowledge.md`); its section is the
  // URL's anchor.
  help: ["page"],
};

/** The key naming the namespace a page shows. The app's own addresses carry it first (a
 *  navigation, a switch, boot, an object's link), so a reload or a copied link shows the
 *  same namespace, and a link from elsewhere opens on it (App's boot). Auth-off mode
 *  serves one namespace and names none. */
const NAMESPACE_KEY = "namespace";

/** The namespace the address names: the page's, or the one a link asked for while the
 *  account cannot read it. */
function pageNamespace(): string {
  const s = useStore.getState();
  return s.deniedNamespace ?? s.identity?.namespace ?? "";
}

/** `params` as a query string with `namespace` first, or without it when empty. */
function withNamespace(params: URLSearchParams, namespace = pageNamespace()): string {
  params.delete(NAMESPACE_KEY);
  const rest = params.toString();
  if (!namespace || isAuthDisabled()) return rest;
  const head = new URLSearchParams({ [NAMESPACE_KEY]: namespace }).toString();
  return rest ? `${head}&${rest}` : head;
}

/** The current address naming `namespace`, without the query keys in `drop`. */
function addressFor(namespace: string, drop: readonly string[] = []): string {
  const params = new URLSearchParams(window.location.search);
  for (const key of drop) params.delete(key);
  const qs = withNamespace(params, namespace);
  return `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`;
}

/** The namespace a link names (`?namespace=`), or null when the value is no namespace name
 *  (`*`, a sentence): the page never prints text a link made up. */
export function linkedNamespace(): string | null {
  const ns = urlParam(NAMESPACE_KEY);
  return ns && NAMESPACE_NAME.test(ns) ? ns : null;
}

/** A link whose `?namespace=` is a collection opens that collection in the Library: no other
 *  view reads one, and the header never holds one. The address is rewritten in place (a
 *  Library link keeps its own `?collection=` and `?doc=`), and the view to boot is returned. */
export function collectionLinkView(view: View | null): View | null {
  const ns = linkedNamespace();
  if (!isCollectionName(ns)) return view;
  const kept = new URLSearchParams(view === "knowledge" ? window.location.search : "");
  kept.delete(NAMESPACE_KEY);
  // The root's `?view=` named the page the path now names.
  kept.delete("view");
  // A link naming the collection as its namespace: its `?doc=` is a document in it.
  if (!isCollectionName(kept.get("collection"))) kept.set("collection", ns);
  patchEntry({}, `${viewHref("knowledge")}?${kept}${window.location.hash}`);
  return "knowledge";
}

/** How a navigation goes. `query`: the keys the destination opens on (`{ seed }` for a
 *  walk), in place of the ones the current URL carries.
 *  `back`: what the person left, when the jump offers a way back to it ("memory
 *  api/rate-limit") — the destination shows "Back to <back>". `focusKey`: the element Back
 *  focuses when it is not the focused one. `note`: what the arrival says, in place of the
 *  page's title. */
export interface NavOptions {
  query?: Record<string, string>;
  back?: string;
  focusKey?: string;
  note?: string;
  /** The element the arrival focuses, by its `data-focus-key`, in place of the page's h1
   *  (`arriveAt`); `focusKey` stays the one Back focuses. */
  arriveFocus?: string;
}

/** The URL for `v`: the page's namespace, then with `query` exactly those keys, else only
 *  the current query keys `v` owns. */
export function navigationHref(v: View, query?: Record<string, string>): string {
  const want = viewHref(v);
  if (typeof window === "undefined") return want;
  const kept = new URLSearchParams(query);
  if (!query) {
    const current = new URLSearchParams(window.location.search);
    for (const key of VIEW_QUERY_KEYS[v] ?? []) {
      const value = current.get(key);
      if (value != null) kept.set(key, value);
    }
  }
  const qs = withNamespace(kept);
  return qs ? `${want}?${qs}` : want;
}

/** A new history entry for `v`. The entry being left first remembers the place (scroll,
 *  focus, the open memory) so Back can restore it; skipped for a no-op. */
function pushView(v: View, opts: NavOptions = {}, remember = true): void {
  if (typeof window === "undefined") return;
  const want = navigationHref(v, opts.query);
  if (window.location.pathname + window.location.search === want) return;
  // Boot (`remember` off) only rewrites the address it arrived at, and the entry keeps
  // its state.
  if (!remember) return patchEntry({}, want + window.location.hash);
  const s = useStore.getState();
  // The phone's assistant sheet has an entry of its own over the page (its Back closes it):
  // a navigation from it replaces that entry, so Back from the new page returns to the page
  // under the sheet, which remembered its place when the sheet opened.
  const replace = sheetEntry();
  if (!replace)
    rememberPlace({ key: s.inspectedMemoryKey, agent: s.inspectedAgent, owner: s.inspectedOwner }, opts.focusKey);
  // A navigation's entry is made in the current namespace.
  const namespace = s.identity?.namespace ?? null;
  // `loomFromApp`: Back from this entry stays in the app (the assistant's Collapse).
  const state = {
    loomFromApp: true,
    ...(opts.back ? { loomBack: opts.back } : {}),
    ...(namespace !== null ? { loomNamespace: namespace } : {}),
  };
  if (replace) history.replaceState(state, "", want);
  else history.pushState(state, "", want);
}

/** Whether the current history entry is the one the assistant's phone sheet pushed. */
export function sheetEntry(): boolean {
  const st: unknown = typeof history === "undefined" ? null : history.state;
  return !!st && typeof st === "object" && (st as Record<string, unknown>).loomSheet === true;
}

export function urlParam(key: string): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get(key);
}

/** Set (or with null, drop) one query key of the current entry, without a new entry: opening
 *  and closing the object a page shows is not a navigation. `state` merges into the
 *  entry's state. */
export function setViewParam(key: string, value: string | null, state: Record<string, unknown> = {}): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  if (value === null) params.delete(key);
  else params.set(key, value);
  const qs = params.toString();
  patchEntry(state, `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`);
}

/** The writer agent a `?memory=` link opens under: the one the entry remembers (a reload
 *  keeps history.state), else every agent ("*", as Runs opens a key it only knows by name). */
function linkedAgent(key: string): string {
  const s: unknown = typeof history === "undefined" ? null : history.state;
  const remembered = s && typeof s === "object" ? (s as Record<string, unknown>).loomInspect : null;
  if (remembered && typeof remembered === "object") {
    const r = remembered as Record<string, unknown>;
    if (r.key === key && typeof r.agent === "string") return r.agent;
  }
  return "*";
}

// The URL keys that name an object of one namespace: a switch drops them.
const SWITCH_CLEARS = ["memory", "thread", "doc", "task"];
// A collection is the org's, not a namespace's: an entry made in another namespace keeps it,
// and only the person's own switch of namespace leaves it.
const PICK_CLEARS = [...SWITCH_CLEARS, "collection"];

/** The namespace the current history entry was made in: the app's stamp, else the one its
 *  address names (the entry a link opened, which boot rewrites and never stamps). */
function entryNamespace(): string | null {
  const s: unknown = typeof history === "undefined" ? null : history.state;
  const ns = s && typeof s === "object" ? (s as Record<string, unknown>).loomNamespace : null;
  return typeof ns === "string" ? ns : urlParam(NAMESPACE_KEY);
}

/** A document the entry opens in a collection: the collection's, like the collection itself,
 *  so an entry made in another namespace keeps it. */
function collectionDoc(view: View): string | null {
  return view === "knowledge" && isCollectionName(urlParam("collection")) ? urlParam("doc") : null;
}

/** Whether what the current entry names belongs to `namespace`: an entry made in another
 *  reopens nothing (a memory, a document, a thread) under this one. */
function entryHere(namespace: string | null): boolean {
  const ns = entryNamespace();
  return ns === null || ns === namespace;
}

/** Back and Forward never switch namespace: the address names the page's, and nothing an
 *  entry made in another names. A move inside one view (the Graph's walks) calls it too. */
export function addressPageNamespace(): void {
  const namespace = useStore.getState().identity?.namespace ?? null;
  const here = entryHere(namespace);
  const keepDoc = !here && collectionDoc(useStore.getState().view) !== null;
  const want = addressFor(pageNamespace(), here ? [] : SWITCH_CLEARS.filter((k) => !(keepDoc && k === "doc")));
  // An entry made in another namespace becomes this one's, so what is opened on it next
  // reopens after a reload, and what it remembered of the other never does.
  if (!here || want !== window.location.pathname + window.location.search + window.location.hash)
    patchEntry(here ? {} : stampFor(namespace), want);
}

/** The entry state that makes the current entry `namespace`'s: stamped with it, and
 *  remembering no memory of the namespace it was made in. */
function stampFor(namespace: string | null): Record<string, unknown> {
  const place = readPlace(typeof history === "undefined" ? null : history.state);
  return {
    loomNamespace: namespace,
    loomInspect: null,
    ...(place ? { loomPlace: { ...place, inspected: null, inspectedAgent: "", inspectedOwner: null } } : {}),
  };
}

/** What the URL opens on view `v`: the memory, document or thread its query names. */
function openedByUrl(v: View): Partial<AppState> {
  const memory = v === "memories" ? urlParam("memory") : null;
  const doc = v === "knowledge" ? urlParam("doc") : null;
  const thread = v === "sessions" ? urlParam("thread") : null;
  return {
    inspectedMemoryKey: memory,
    inspectedAgent: memory ? linkedAgent(memory) : "",
    inspectedOwner: null,
    ...(doc ? { pendingDocSourceUri: doc } : {}),
    ...(thread ? { pendingConversationThread: { threadId: thread } } : {}),
  };
}

/** Resolve the current view from window.location.pathname. The bare root has none unless
 *  its query names one, by id or by path (`?view=memories`, `?view=admin/health`); a
 *  `?memory=` link opens on Memories. */
export function pathnameView(): View | null {
  if (typeof window === "undefined") return null;
  const p = window.location.pathname;
  const m = p.match(/^\/(.+?)\/?$/);
  if (m) return pathToView(m[1]);
  const v = urlParam("view");
  if (v) return ALL_VIEWS.find((x) => x === v) ?? pathToView(v);
  return urlParam("memory") ? "memories" : null;
}

/** Why the sign-in gate is showing: a sign-in session's token the server no longer
 *  accepts is "expired" (SSO resumes by itself), any other stored token "ended"; a token a
 *  sign-in link brought that it refused is "refused"; a first visit carries null. */
export type GateReason = "expired" | "ended" | "refused" | null;

interface AppState {
  authed: boolean;
  whoami: WhoAmI | null;
  identity: Identity | null;
  gateReason: GateReason;
  /** The server refused the stored token mid-session: a card among the toasts says so and
   *  offers the sign-in, and the page stays as it is, so nothing typed into it is lost. */
  sessionExpired: boolean;
  /** The caller's namespaces with roles (`namespace.list_mine`), set once at
   *  boot. null = not fetched / the fetch failed, which the role helpers
   * treat as "unknown", not "no access". */
  myNamespaces: MyNamespaceEntry[] | null;
  /** The namespace a link named that the account cannot read. The page says so, over the
   *  namespace a visit without the link opens, until a navigation or a pick leaves it. */
  deniedNamespace: string | null;
  /** The namespace a link opened for this tab: never saved as the pick, so what saves a
   *  namespace on the way back from elsewhere (a tracker's Patterns board) skips it. A
   *  switch ends it, except a return to it (`setLinkOpened`). */
  linkOpened: string | null;
  /** The namespace a switch back returns to is the link's again (the tracker's Back). */
  setLinkOpened: (namespace: string | null) => void;
  view: View;
  /** Counts user navigations (setView and the open-X helpers), never the boot's
   *  application of the URL's view nor Back/Forward; the shell's scroll reset and its
   *  focus move key on it. */
  navSeq: number;
  /** What the latest navigation's arrival says, when not just the page's title. */
  navNote: string | null;
  /** The element the latest navigation's arrival focuses (`NavOptions.arriveFocus`). */
  arriveFocus: string | null;
  inspectedMemoryKey: string | null;
  // Writer-identity override for the inspected memory: when the list ran
  // with agent="*", the writer's agent may differ from identity.agent and
  // a strict memory.get would 404. Empty string falls back to identity.agent.
  inspectedAgent: string;
  // The user and session the inspected row is filed under, when the view that opened it
  // names them (the Assistant's inspected and cited rows); null reads the caller's own.
  inspectedOwner: InspectedOwner | null;
  // Tag-chip navigation target; Recent.tsx filters by it.
  memoryTagFilter: string | null;
  // Header-search document target; Documents.tsx opens its preview dialog.
  pendingDocSourceUri: string | null;
  /** A thread to open on Conversations. */
  pendingConversationThread: { threadId: string } | null;
  // Header-search profile-section target; App.tsx opens ProfileSectionDialog.
  inspectedProfileLabel: string | null;
  // Profile section to open directly in edit mode after navigation.
  pendingEditProfileLabel: string | null;
  /** The run whose recorded searches the Recall history tab shows; set by a run's replay,
   *  cleared by the tab's chip. */
  recallRunFilter: string | null;

  // actions
  setAuthed: (v: boolean) => void;
  setGateReason: (r: GateReason) => void;
  setSessionExpired: (v: boolean) => void;
  setMyNamespaces: (rows: MyNamespaceEntry[] | null) => void;
  setWhoami: (w: WhoAmI | null) => void;
  setIdentity: (i: Identity | null) => void;
  /** Boot's identity, provisional or settled: not a switch, so the URL's object stays, and
   *  an equal identity keeps its object (views key their reads on it). `denied`: the
   *  namespace the link named, when the account cannot read it; `linkOpened`: the one it
   *  opened. */
  bootIdentity: (i: Identity | null, denied?: string | null, linkOpened?: string | null) => void;
  setView: (v: View) => void;
  /** A navigation to `v` with a query, a way back or an arrival note (see NavOptions). */
  navigate: (v: View, opts?: NavOptions) => void;
  /** The boot applying the view the URL names (or a fallback) — not a navigation. */
  bootView: (v: View) => void;
  /** Back or Forward reached `v`: the view, and the memory the entry had open — from its
   *  remembered place, else from its URL. Not a navigation: nothing is pushed. */
  arriveFromHistory: (v: View, place: Place | null) => void;
  /** Ask the header's namespace picker to open (the no-namespace landing opens it so the
   *  user picks one). The picker clears the flag once it has opened. */
  namespacePickerOpen: boolean;
  openNamespacePicker: (v: boolean) => void;
  inspect: (memoryKey: string | null, opts?: { agent?: string; userId?: string; sessionId?: string }) => void;
  helpOpen: boolean;
  openHelp: (v: boolean) => void;
  toggleSidebar: () => void;
  sidebarCollapsed: boolean;
  /** Navigate to Memories filtered by tag. */
  searchByTag: (tag: string) => void;
  setMemoryTagFilter: (tag: string | null) => void;
  openDocument: (sourceUri: string) => void;
  /** Open a page of Loom's docs on the Help view, at a section's anchor when given. */
  openDocsPage: (page: string, anchor?: string) => void;
  clearPendingDoc: () => void;
  /** The Library shows a place other than the header's namespace (a collection, or one it
   *  is confirming), or shows nothing of a namespace that holds no documents: the header's
   *  New › Document, which adds to the header's namespace, steps aside then. */
  libraryElsewhere: boolean;
  setLibraryElsewhere: (elsewhere: boolean) => void;
  openConversationThread: (threadId: string) => void;
  clearPendingConversationThread: () => void;
  inspectProfile: (label: string | null) => void;
  requestEditProfile: (label: string | null) => void;
  /** Open Memories → Recall → history, filtered to one run. */
  openRecallForRun: (runId: string) => void;
  setRecallRunFilter: (runId: string | null) => void;

  // Bumped on memory writes so the active list view re-fetches immediately.
  memoriesVersion: number;
  bumpMemoriesVersion: () => void;

  /** Ask the header's ⌘K palette to open with `hint` typed in (the checklist's "Find it
   *  again" step opens the existing palette instead of a search of its own). The palette
   *  clears the request once it has honoured it; `nonce` lets the same hint be requested
   *  twice. */
  paletteRequest: { hint: string; nonce: number } | null;
  openPalette: (hint: string) => void;
  clearPaletteRequest: () => void;
  /** How many times the palette has opened this session — the checklist's
   * "Find it again" step ticks on the first. */
  paletteOpens: number;
  notePaletteOpened: () => void;
}

export const useStore = create<AppState>((set, get) => ({
  // Seed from auth-presence so a refresh skips the login gate: a stored token
  // (or proxy mode) means "probably authed" → first paint shows the app's
  // "Loading…" splash, not TokenGate, while whoami confirms. whoami still
  // flips this to false (→ gate) if the token turns out invalid/expired.
  authed: isProxyMode() || hasToken(),
  whoami: null,
  identity: null,
  gateReason: null,
  sessionExpired: false,
  myNamespaces: null,
  deniedNamespace: null,
  linkOpened: null,
  setLinkOpened: (linkOpened) => set({ linkOpened }),
  view: "memories",
  navSeq: 0,
  navNote: null,
  arriveFocus: null,
  inspectedMemoryKey: null,
  inspectedAgent: "",
  inspectedOwner: null,
  memoryTagFilter: null,
  pendingDocSourceUri: null,
  pendingConversationThread: null,
  inspectedProfileLabel: null,
  pendingEditProfileLabel: null,
  recallRunFilter: null,
  sidebarCollapsed: typeof localStorage !== "undefined" && localStorage.getItem("loom_sidebar_collapsed") === "1",
  setAuthed: (authed) => set({ authed }),
  setGateReason: (gateReason) => set({ gateReason }),
  setSessionExpired: (sessionExpired) => set({ sessionExpired }),
  setMyNamespaces: (myNamespaces) => set({ myNamespaces }),
  setWhoami: (whoami) => set({ whoami }),
  // A memory belongs to its namespace: a switch, from any picker, closes the open one. The
  // address names the new namespace and drops the keys that name an object of the one left
  // (`?memory=`, `?thread=`, `?doc=`, `?task=`, `?collection=`), and the entry is stamped with the new
  // namespace and forgets the memory it remembered, so neither a reload nor Back reopens
  // the old namespace's object here. Any pick leaves a link's unreadable namespace, the
  // namespace the page already shows included.
  setIdentity: (identity) => {
    const s = get();
    if (s.identity && identity && (s.identity.namespace !== identity.namespace || s.deniedNamespace)) {
      patchEntry(stampFor(identity.namespace), addressFor(identity.namespace, PICK_CLEARS));
      // The Recall Lab's run filter names a run of the namespace left.
      set({
        identity,
        deniedNamespace: null,
        linkOpened: null,
        inspectedMemoryKey: null,
        pendingConversationThread: null,
        pendingDocSourceUri: null,
        recallRunFilter: null,
      });
    } else set({ identity });
  },
  bootIdentity: (identity, denied = null, linkOpened = null) =>
    set((s) => ({
      identity: identity && sameIdentity(s.identity, identity) ? s.identity : identity,
      deniedNamespace: denied,
      linkOpened,
    })),
  setView: (view) => get().navigate(view),
  navigate: (view, opts = {}) => {
    // A navigation leaves a link's unreadable namespace for the one the picker shows.
    if (get().deniedNamespace) get().setIdentity(get().identity);
    // Mirror the view into the URL first (skipping no-op pushes): the entry being left
    // remembers the open memory before it is cleared.
    pushView(view, opts);
    // A navigation closes the memory the drawer had open; on Memories itself the URL is the
    // same page, so its `?memory=` goes too, or a reload would reopen what was closed.
    if (!opts.query && urlParam("memory") !== null) setViewParam("memory", null, { loomInspect: null });
    set((s) => ({
      view,
      inspectedMemoryKey: null,
      navSeq: s.navSeq + 1,
      navNote: opts.note ?? null,
      arriveFocus: opts.arriveFocus ?? null,
    }));
  },
  bootView: (view) => {
    pushView(view, {}, false);
    // What a link names belongs to its namespace: none of it opens on another, and nothing
    // opens under the page that says the account cannot read the link's namespace.
    const here = !get().deniedNamespace && entryHere(get().identity?.namespace ?? null);
    set({
      view,
      ...(here ? openedByUrl(view) : { inspectedMemoryKey: null, inspectedAgent: "", inspectedOwner: null }),
    });
  },
  arriveFromHistory: (view, place) => {
    // An entry made in another namespace reopens nothing it names in this one.
    const here = entryHere(get().identity?.namespace ?? null);
    const doc = collectionDoc(view);
    const opened = here ? openedByUrl(view) : doc ? { pendingDocSourceUri: doc } : {};
    const inspected = here ? (place?.inspected ?? null) : null;
    set({
      view,
      ...opened,
      inspectedMemoryKey: inspected ?? opened.inspectedMemoryKey ?? null,
      inspectedAgent: inspected ? (place?.inspectedAgent ?? "") : (opened.inspectedAgent ?? ""),
      inspectedOwner: inspected ? (place?.inspectedOwner ?? null) : null,
    });
    addressPageNamespace();
  },
  namespacePickerOpen: false,
  openNamespacePicker: (namespacePickerOpen) => set({ namespacePickerOpen }),
  inspect: (inspectedMemoryKey, opts) => {
    const { identity, deniedNamespace } = get();
    // A memory opens in the namespace the page reads (the palette searches that one): a
    // link's unreadable namespace is left first, as Continue leaves it.
    if (inspectedMemoryKey && deniedNamespace && identity) get().setIdentity(identity);
    const agent = opts?.agent || "";
    const owner =
      opts?.userId || opts?.sessionId ? { user_id: opts.userId || "-", session_id: opts.sessionId || "-" } : null;
    set({ inspectedMemoryKey, inspectedAgent: agent, inspectedOwner: owner });
    // On Memories the open memory is the page's URL (`?memory=`), with the agent it was
    // read as in the entry's state for a reload.
    if (get().view === "memories") {
      setViewParam("memory", inspectedMemoryKey, {
        loomInspect: inspectedMemoryKey ? { key: inspectedMemoryKey, agent } : null,
      });
    }
  },
  helpOpen: false,
  openHelp: (helpOpen) => set({ helpOpen }),
  searchByTag: (tag) => {
    get().navigate("memories");
    set({ memoryTagFilter: tag });
  },
  setMemoryTagFilter: (tag) => set({ memoryTagFilter: tag }),
  openDocument: (sourceUri) => {
    get().navigate("knowledge", { query: { doc: sourceUri } });
    set({ pendingDocSourceUri: sourceUri });
  },
  clearPendingDoc: () => set({ pendingDocSourceUri: null }),
  openDocsPage: (page, anchor = "") => {
    const before = window.location.pathname + window.location.search;
    get().navigate("help", { query: { page } });
    // The section rides the URL's anchor, which the Help view scrolls to once it has read
    // the page; navSeq moves on either way, so the view reads the address again. A move to
    // another section of the same page gets its own entry, so Back returns to the first.
    const here = window.location.pathname + window.location.search;
    const hash = anchor ? `#${anchor}` : "";
    if (here === before && hash !== window.location.hash) history.pushState(history.state, "", here + hash);
    else history.replaceState(history.state, "", here + hash);
  },
  openConversationThread: (threadId) => {
    get().navigate("sessions", { query: { thread: threadId } });
    set({ pendingConversationThread: { threadId } });
  },
  clearPendingConversationThread: () => set({ pendingConversationThread: null }),
  inspectProfile: (inspectedProfileLabel) => set({ inspectedProfileLabel }),
  openRecallForRun: (runId) => {
    get().navigate("recall");
    set({ recallRunFilter: runId });
  },
  setRecallRunFilter: (recallRunFilter) => set({ recallRunFilter }),
  requestEditProfile: (pendingEditProfileLabel) => {
    get().navigate("profile");
    set({ pendingEditProfileLabel, inspectedProfileLabel: null });
  },
  memoriesVersion: 0,
  bumpMemoriesVersion: () => set((s) => ({ memoriesVersion: s.memoriesVersion + 1 })),
  libraryElsewhere: false,
  setLibraryElsewhere: (libraryElsewhere) => set({ libraryElsewhere }),
  paletteRequest: null,
  openPalette: (hint) => set((s) => ({ paletteRequest: { hint, nonce: (s.paletteRequest?.nonce ?? 0) + 1 } })),
  clearPaletteRequest: () => set({ paletteRequest: null }),
  paletteOpens: 0,
  notePaletteOpened: () => set((s) => ({ paletteOpens: s.paletteOpens + 1 })),
  toggleSidebar: () =>
    set((state) => {
      const next = !state.sidebarCollapsed;
      try {
        localStorage.setItem("loom_sidebar_collapsed", next ? "1" : "0");
      } catch {
        /* quota */
      }
      return { sidebarCollapsed: next };
    }),
}));

/**
 * The caller's identity, for components App renders only once identity is set
 * (its gate is `identity && whoami`, and nothing sets identity back to null).
 * The invariant is checked here once; outside the gate this throws a named
 * error rather than handing null to the caller.
 */
export function useIdentity(): Identity {
  const identity = useStore((s) => s.identity);
  if (!identity) throw new Error("useIdentity: no identity (rendered outside App's identity gate)");
  return identity;
}
