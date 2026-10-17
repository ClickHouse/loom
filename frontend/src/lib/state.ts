import { create } from "zustand";
import type { Identity, MyNamespaceEntry, WhoAmI } from "../api";
import { hasToken, isProxyMode } from "../api";

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
};

/** The URL for `v`, keeping only the current query keys `v` owns. */
export function navigationHref(v: View): string {
  const want = viewHref(v);
  if (typeof window === "undefined") return want;
  const owned = VIEW_QUERY_KEYS[v] ?? [];
  const current = new URLSearchParams(window.location.search);
  const kept = new URLSearchParams();
  for (const key of owned) {
    const value = current.get(key);
    if (value != null) kept.set(key, value);
  }
  const qs = kept.toString();
  return qs ? `${want}?${qs}` : want;
}

function pushView(v: View): void {
  if (typeof window === "undefined") return;
  const want = navigationHref(v);
  if (window.location.pathname + window.location.search !== want) {
    history.pushState(null, "", want);
  }
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
   *  application of the URL's view; the shell's scroll reset keys on it. */
  navSeq: number;
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
  /** The boot applying the view the URL names (or a fallback) — not a navigation. */
  bootView: (v: View) => void;
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

export const useStore = create<AppState>((set) => ({
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
  setIdentity: (identity) => set({ identity }),
  setView: (view) => {
    set((s) => ({ view, inspectedMemoryKey: null, navSeq: s.navSeq + 1 }));
    // Mirror the view into the URL; skip no-op pushes.
    pushView(view);
  },
  bootView: (view) => {
    set({ view, inspectedMemoryKey: null });
    pushView(view);
  },
  namespacePickerOpen: false,
  openNamespacePicker: (namespacePickerOpen) => set({ namespacePickerOpen }),
  inspect: (inspectedMemoryKey, opts) => set({ inspectedMemoryKey, inspectedAgent: opts?.agent || "" }),
  helpOpen: false,
  openHelp: (helpOpen) => set({ helpOpen }),
  searchByTag: (tag) => {
    set((s) => ({
      view: "memories",
      inspectedMemoryKey: null,
      memoryTagFilter: tag,
      navSeq: s.navSeq + 1,
    }));
    pushView("memories");
  },
  setMemoryTagFilter: (tag) => set({ memoryTagFilter: tag }),
  openDocument: (sourceUri) => {
    set((s) => ({ view: "knowledge", pendingDocSourceUri: sourceUri, navSeq: s.navSeq + 1 }));
    pushView("knowledge");
  },
  clearPendingDoc: () => set({ pendingDocSourceUri: null }),
  openConversationThread: (threadId, agent = "") => {
    set((s) => ({
      view: "sessions",
      pendingConversationThread: { threadId, agent },
      inspectedMemoryKey: null,
      navSeq: s.navSeq + 1,
    }));
    pushView("sessions");
  },
  clearPendingConversationThread: () => set({ pendingConversationThread: null }),
  inspectProfile: (inspectedProfileLabel) => set({ inspectedProfileLabel }),
  openRecallForRun: (runId) => {
    set((s) => ({ view: "recall", recallRunFilter: runId, inspectedMemoryKey: null, navSeq: s.navSeq + 1 }));
    pushView("recall");
  },
  setRecallRunFilter: (recallRunFilter) => set({ recallRunFilter }),
  requestEditProfile: (pendingEditProfileLabel) => {
    set((s) => ({
      pendingEditProfileLabel,
      inspectedProfileLabel: null,
      view: "profile",
      navSeq: s.navSeq + 1,
    }));
    pushView("profile");
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
