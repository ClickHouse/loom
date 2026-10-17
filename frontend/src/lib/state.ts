import { create } from "zustand";
import type { Identity, MyNamespaceEntry, WhoAmI } from "../api";
import { hasToken, isProxyMode } from "../api";
import { patchEntry, rememberPlace, type Place } from "./place";

export type View =
  | "memories"
  | "recall"
  | "profile"
  | "knowledge"
  | "sharing"
  | "sessions"
  | "runs"
  | "graph"
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
  | "admin:users";

export const ALL_VIEWS: View[] = [
  "memories",
  "recall",
  "profile",
  "knowledge",
  "sharing",
  "sessions",
  "runs",
  "graph",
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
  // Memories also carries its tab: `?section=insights` is the Insights tab (MemoryTabs).
  memories: ["memory", "section"],
  knowledge: ["doc"],
  sessions: ["thread"],
};

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
}

/** The URL for `v`: with `query`, exactly those keys; else only the current query keys `v`
 *  owns. */
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
  const qs = kept.toString();
  return qs ? `${want}?${qs}` : want;
}

/** A new history entry for `v`. The entry being left first remembers the place (scroll,
 *  focus, the open memory) so Back can restore it; skipped for a no-op. */
function pushView(v: View, opts: NavOptions = {}, remember = true): void {
  if (typeof window === "undefined") return;
  const want = navigationHref(v, opts.query);
  if (window.location.pathname + window.location.search !== want) {
    if (remember) {
      const s = useStore.getState();
      rememberPlace({ key: s.inspectedMemoryKey, agent: s.inspectedAgent }, opts.focusKey);
    }
    history.pushState(opts.back ? { loomBack: opts.back } : null, "", want);
  }
}

function urlParam(key: string): string | null {
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

/** What the URL opens on view `v`: the memory, document or thread its query names. */
function openedByUrl(v: View): Partial<AppState> {
  const memory = v === "memories" ? urlParam("memory") : null;
  const doc = v === "knowledge" ? urlParam("doc") : null;
  const thread = v === "sessions" ? urlParam("thread") : null;
  return {
    inspectedMemoryKey: memory,
    inspectedAgent: memory ? linkedAgent(memory) : "",
    ...(doc ? { pendingDocSourceUri: doc } : {}),
    ...(thread ? { pendingConversationThread: { threadId: thread, agent: "" } } : {}),
  };
}

/** Resolve the current view from window.location.pathname; null for the bare root. */
export function pathnameView(): View | null {
  if (typeof window === "undefined") return null;
  const p = window.location.pathname;
  const m = p.match(/^\/(.+?)\/?$/);
  if (!m) return null;
  return pathToView(m[1]);
}

/** Why the sign-in gate is showing: a stored token the server no longer accepts is
 *  "expired"; a first visit carries null. */
export type GateReason = "expired" | null;

interface AppState {
  authed: boolean;
  whoami: WhoAmI | null;
  identity: Identity | null;
  gateReason: GateReason;
  /** The caller's namespaces with roles (`namespace.list_mine`), set once at
   *  boot. null = not fetched / the fetch failed, which the role helpers
   * treat as "unknown", not "no access". */
  myNamespaces: MyNamespaceEntry[] | null;
  view: View;
  /** Counts user navigations (setView and the open-X helpers), never the boot's
   *  application of the URL's view nor Back/Forward; the shell's scroll reset and its
   *  focus move key on it. */
  navSeq: number;
  /** What the latest navigation's arrival says, when not just the page's title. */
  navNote: string | null;
  inspectedMemoryKey: string | null;
  // Writer-identity override for the inspected memory: when the list ran
  // with agent="*", the writer's agent may differ from identity.agent and
  // a strict memory.get would 404. Empty string falls back to identity.agent.
  inspectedAgent: string;
  // Tag-chip navigation target; Recent.tsx filters by it.
  memoryTagFilter: string | null;
  // Header-search document target; Documents.tsx opens its preview dialog.
  pendingDocSourceUri: string | null;
  /** A thread to open on Conversations, with the agent its link knows (or ""). */
  pendingConversationThread: { threadId: string; agent: string } | null;
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
  setMyNamespaces: (rows: MyNamespaceEntry[] | null) => void;
  setWhoami: (w: WhoAmI | null) => void;
  setIdentity: (i: Identity | null) => void;
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
  inspect: (memoryKey: string | null, opts?: { agent?: string }) => void;
  helpOpen: boolean;
  openHelp: (v: boolean) => void;
  toggleSidebar: () => void;
  sidebarCollapsed: boolean;
  /** Navigate to Memories filtered by tag. */
  searchByTag: (tag: string) => void;
  setMemoryTagFilter: (tag: string | null) => void;
  openDocument: (sourceUri: string) => void;
  clearPendingDoc: () => void;
  openConversationThread: (threadId: string, agent?: string) => void;
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
  myNamespaces: null,
  view: "memories",
  navSeq: 0,
  navNote: null,
  inspectedMemoryKey: null,
  inspectedAgent: "",
  memoryTagFilter: null,
  pendingDocSourceUri: null,
  pendingConversationThread: null,
  inspectedProfileLabel: null,
  pendingEditProfileLabel: null,
  recallRunFilter: null,
  sidebarCollapsed: typeof localStorage !== "undefined" && localStorage.getItem("loom_sidebar_collapsed") === "1",
  setAuthed: (authed) => set({ authed }),
  setGateReason: (gateReason) => set({ gateReason }),
  setMyNamespaces: (myNamespaces) => set({ myNamespaces }),
  setWhoami: (whoami) => set({ whoami }),
  // A memory belongs to its namespace: a switch, from any picker, closes the open one.
  setIdentity: (identity) =>
    set((s) =>
      s.identity && identity && s.identity.namespace !== identity.namespace
        ? { identity, inspectedMemoryKey: null }
        : { identity },
    ),
  setView: (view) => get().navigate(view),
  navigate: (view, opts = {}) => {
    // Mirror the view into the URL first (skipping no-op pushes): the entry being left
    // remembers the open memory before it is cleared.
    pushView(view, opts);
    // A navigation closes the memory the drawer had open; on Memories itself the URL is the
    // same page, so its `?memory=` goes too, or a reload would reopen what was closed.
    if (!opts.query && urlParam("memory") !== null) setViewParam("memory", null, { loomInspect: null });
    // Its tab too: a link to Memories means the list (the tabs set `section` themselves).
    if (!opts.query && view === "memories" && urlParam("section") !== null) setViewParam("section", null);
    set((s) => ({ view, inspectedMemoryKey: null, navSeq: s.navSeq + 1, navNote: opts.note ?? null }));
  },
  bootView: (view) => {
    pushView(view, {}, false);
    set({ view, ...openedByUrl(view) });
  },
  arriveFromHistory: (view, place) => {
    const opened = openedByUrl(view);
    set({
      view,
      ...opened,
      inspectedMemoryKey: place?.inspected ?? opened.inspectedMemoryKey ?? null,
      inspectedAgent: place?.inspected ? place.inspectedAgent : (opened.inspectedAgent ?? ""),
    });
  },
  namespacePickerOpen: false,
  openNamespacePicker: (namespacePickerOpen) => set({ namespacePickerOpen }),
  inspect: (inspectedMemoryKey, opts) => {
    const agent = opts?.agent || "";
    set({ inspectedMemoryKey, inspectedAgent: agent });
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
  openConversationThread: (threadId, agent = "") => {
    get().navigate("sessions", { query: { thread: threadId } });
    set({ pendingConversationThread: { threadId, agent } });
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
