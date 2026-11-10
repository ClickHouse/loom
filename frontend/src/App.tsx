import { Suspense, lazy, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { flushView, installVitals, startView } from "./lib/vitals";
import { ReloadBar } from "./components/ReloadBar";
import { sharedMine, sharedHealth } from "./lib/sharedReads";
import { SkeletonCard } from "./components/Skeleton";
import { Loading, LoadingSkeleton } from "./components/ui";
import {
  getSavedNamespace,
  defaultNamespace,
  isProxyMode,
  isAuthDisabled,
  listGrants,
  setToken,
  hasToken,
  clearToken,
  onSignInRejected,
  onTokenStored,
  setPageOwner,
  signedOut,
  storedToken,
  type MyNamespaceEntry,
  type WhoAmI,
} from "./api";
import {
  canReadNamespace,
  deepLinkNamespace,
  isCollectionName,
  isMemoryNamespace,
  landingTarget,
  needsMemoryNamespace,
} from "./lib/namespace";
import {
  claimsFromToken,
  provisionalNamespace,
  rememberKinds,
  sameAccount,
  takeLinkToken,
  takeSignInStarted,
  takeSsoAsk,
} from "./lib/boot";
import { pendingKey } from "./lib/pendingKey";
import { preloadView } from "./views/registry";
import { useStore, pathnameView, linkedNamespace, addressPageNamespace, collectionLinkView } from "./lib/state";
import { arriveAt, readPlace, restorePlace } from "./lib/place";
import { useKeyboard } from "./lib/hooks";
import { NetworkProgress } from "./components/NetworkProgress";
import { Sidebar } from "./layout/Sidebar";
import { Header } from "./layout/Header";
import { MobileNav } from "./layout/MobileNav";
import { ToastViewport, toast, toastError } from "./components/Toast";
import { ConfirmViewport } from "./components/ConfirmDialog";
import { PendingRequestBanner } from "./components/PendingRequestBanner";
import { shortcutOff } from "./lib/shortcuts";
import { assistantPopstate, bootDock, onAssistantShortcut, showExpanded, useDock } from "./lib/assistantDock";

import { reloadPage } from "./lib/navigation";
import { endSession, lostReason, sessionEndedNote, whoamiWhenReady } from "./lib/session";

onSignInRejected(endSession);
// A call carries only the token of the person whose page this is.
useStore.subscribe((s) => setPageOwner(s.whoami));
// A sign-in, a dropped token or a sign-out in another tab reaches this one from the first
// moment (a proxy's header and an auth-off install keep no token; before boot has an
// identity, boot's own checks decide).
// Its handler ships in the session card's chunk; one that cannot load reloads, and the
// fresh page decides on the stored token.
onTokenStored((token, before) => {
  if (!isProxyMode() && !isAuthDisabled())
    void sessionCard.then((m) => (m?.tokenChangedElsewhere ?? reloadPage)(token, before));
});

// One view per chunk, so a person opening Memories does not pay for the code index
// explorer, the graph and every admin page. React.lazy loads a view the first time it is
// shown; the shell (header, sidebar, store) is what every page pays for.
const Recent = lazy(() => import("./views/Recent").then((m) => ({ default: m.Recent })));
const RecallLab = lazy(() => import("./views/RecallLab").then((m) => ({ default: m.RecallLab })));
const Profile = lazy(() => import("./views/Profile").then((m) => ({ default: m.Profile })));
const Documents = lazy(() => import("./views/Documents").then((m) => ({ default: m.Documents })));
const Conversations = lazy(() => import("./views/Conversations").then((m) => ({ default: m.Conversations })));
const Runs = lazy(() => import("./views/Runs").then((m) => ({ default: m.Runs })));
const GraphExplorer = lazy(() => import("./views/Graph").then((m) => ({ default: m.GraphExplorer })));
const Help = lazy(() => import("./views/Help").then((m) => ({ default: m.Help })));
const Sharing = lazy(() => import("./views/Sharing").then((m) => ({ default: m.Sharing })));
const Browse = lazy(() => import("./views/Browse").then((m) => ({ default: m.Browse })));
const CodeView = lazy(() => import("./views/Code").then((m) => ({ default: m.CodeView })));
const Setup = lazy(() => import("./views/Setup").then((m) => ({ default: m.Setup })));
const Tasks = lazy(() => import("./views/Tasks").then((m) => ({ default: m.Tasks })));
const ReviewQueue = lazy(() => import("./views/admin/ReviewQueue").then((m) => ({ default: m.ReviewQueue })));
const Health = lazy(() => import("./views/admin/Health").then((m) => ({ default: m.Health })));
const Blacklist = lazy(() => import("./views/admin/Blacklist").then((m) => ({ default: m.Blacklist })));
const Policies = lazy(() => import("./views/admin/Policies").then((m) => ({ default: m.Policies })));
const Audit = lazy(() => import("./views/admin/Audit").then((m) => ({ default: m.Audit })));
const Spending = lazy(() => import("./views/admin/Spending").then((m) => ({ default: m.Spending })));
const Agents = lazy(() => import("./views/admin/Agents").then((m) => ({ default: m.Agents })));
const AdminUsersView = lazy(() => import("./views/admin/Users").then((m) => ({ default: m.Users })));
const DeploymentSettings = lazy(() =>
  import("./views/admin/DeploymentSettings").then((m) => ({ default: m.DeploymentSettings })),
);
// The shell's own optional surfaces load when first needed, not with every page. The
// memory inspector (47 kB of source) opens on a card click; the token gate shows only
// when signed out; the two dialogs open on a key or a click.
const MemoryInspector = lazy(() =>
  import("./components/MemoryInspector").then((m) => ({ default: m.MemoryInspector })),
);
const ProfileSectionDialog = lazy(() =>
  import("./flows/ProfileSectionDialog").then((m) => ({ default: m.ProfileSectionDialog })),
);
const HelpDialog = lazy(() => import("./components/HelpDialog").then((m) => ({ default: m.HelpDialog })));
const TokenGate = lazy(() => import("./flows/TokenGate").then((m) => ({ default: m.TokenGate })));
const LinkTokenConfirm = lazy(() => import("./flows/TokenGate").then((m) => ({ default: m.LinkTokenConfirm })));
// The expired-session card's chunk is fetched as the app loads, from the build that served
// it: it shows on a long-lived tab, the one most likely to predate a deploy, and a chunk
// first asked for then would be gone and its stale-chunk reload would lose the page.
// Should that fetch fail, no card shows (each view's error still says the session ended),
// rather than an error page in place of the page.
const sessionCard = import("./components/SessionExpiredBar").catch(() => ({
  default: () => null,
  tokenChangedElsewhere: reloadPage,
}));
// A stale chunk the reload handler (lib/staleChunk.ts) takes resolves to nothing: no card.
const SessionExpiredBar = lazy<() => ReactElement | null>(() =>
  sessionCard.then((m) => ({ default: m?.default ?? (() => null) })),
);
// The health problems banner shows on a degraded deployment only: its own chunk, and none
// (it is best-effort) when that chunk cannot load.
const ProblemsBanner = lazy<() => ReactElement | null>(() =>
  import("./components/ProblemsBanner").then(
    (m) => ({ default: m?.ProblemsBanner ?? (() => null) }),
    () => ({ default: () => null }),
  ),
);

// The assistant panel and its behaviour load the first time it opens (lib/assistantDock.ts).
const AssistantPanel = lazy(() => import("./layout/AssistantPanel").then((m) => ({ default: m.AssistantPanel })));

const NamespaceNoAccess = lazy(() =>
  import("./components/NamespaceNoAccess").then((m) => ({ default: m.NamespaceNoAccess })),
);
// A memory view's page on a code or tracker namespace, or none: a page of its own, as
// the views are.
const MemoryScopeMissing = lazy(() =>
  import("./components/MemoryScopeMissing").then((m) => ({ default: m.MemoryScopeMissing })),
);

export default function App() {
  const authed = useStore((s) => s.authed);
  const whoami_ = useStore((s) => s.whoami);
  const identity = useStore((s) => s.identity);
  const view = useStore((s) => s.view);
  const denied = useStore((s) => s.deniedNamespace);
  // Scroll to top when the view changes — once per navigation, not per commit of <main>:
  // each scrollTo forces a synchronous layout (~40 ms at 4x CPU).
  const mainRef = useRef<HTMLElement | null>(null);
  // Only a navigation scrolls. The boot applying the URL's view is not one — a fresh
  // document is at the top, and a scrollTo there would flush layout for nothing (42 ms at
  // 4x on the Repositories, Graph and Profile boots). `navSeq` counts navigations.
  // Back and Forward do not count: they restore the place the entry remembers (below).
  // A navigation also moves the focus to the new page's h1 and says where it went.
  const navSeq = useStore((s) => s.navSeq);
  useEffect(() => {
    if (navSeq === 0) return;
    mainRef.current?.scrollTo({ top: 0, behavior: "auto" });
    const s = useStore.getState();
    arriveAt(s.navNote, s.arriveFocus);
  }, [navSeq]);
  // The dashboard reports its own paint — one ui.vitals row per view, posted when the
  // view ends (a navigation, the page hiding), never inside the view's own measured
  // window.
  const buildRef = useRef("");
  useEffect(() => {
    installVitals();
  }, []);
  useEffect(() => {
    if (!authed) return;
    sharedHealth()
      .then((h) => {
        buildRef.current = typeof h?.build_commit === "string" ? h.build_commit : "";
      })
      .catch(() => {
        /* unknown build */
      });
  }, [authed]);
  useEffect(() => {
    if (!authed || !identity?.org || !identity?.namespace) return;
    const id = { org: identity.org, namespace: identity.namespace };
    startView(view);
    const onHide = () => flushView(id, buildRef.current);
    window.addEventListener("pagehide", onHide);
    return () => {
      window.removeEventListener("pagehide", onHide);
      flushView(id, buildRef.current);
    };
  }, [view, authed, identity?.org, identity?.namespace]);

  const inspectedKey = useStore((s) => s.inspectedMemoryKey);
  const inspectedAgent = useStore((s) => s.inspectedAgent);
  const inspectedOwner = useStore((s) => s.inspectedOwner);
  // Override agent with the writer's identity (captured when the list-view
  // card was clicked) so strict-tuple memory.get doesn't 404 on cross-agent
  // rows. Memoised: a fresh object every render would replay the inspector's seven reads
  // on any App re-render.
  const inspectorIdentity = useMemo(
    () =>
      identity
        ? {
            ...identity,
            ...(inspectedAgent ? { agent: inspectedAgent } : {}),
            // A row filed under a user or session is read under them, or memory.get misses it.
            ...(inspectedOwner ?? {}),
          }
        : null,
    [identity, inspectedAgent, inspectedOwner],
  );
  const inspect = useStore((s) => s.inspect);
  const setAuthed = useStore((s) => s.setAuthed);
  const gateReason = useStore((s) => s.gateReason);
  const sessionExpired = useStore((s) => s.sessionExpired);
  const setWhoami = useStore((s) => s.setWhoami);
  // Boot settles the identity (a provisional namespace, then the answer): not a switch, so
  // the deep link's memory or thread stays open.
  const bootIdentity = useStore((s) => s.bootIdentity);
  const setView = useStore((s) => s.setView);
  const arriveFromHistory = useStore((s) => s.arriveFromHistory);
  const bootView = useStore((s) => s.bootView);
  const helpOpen = useStore((s) => s.helpOpen);
  const inspectedProfileLabel = useStore((s) => s.inspectedProfileLabel);
  const setHelpOpen = useStore((s) => s.openHelp);
  // A Loom page's ask to start SSO (`?sign_in=sso`), taken from the address at boot.
  const [ssoAsked, setSsoAsked] = useState(false);
  // A link token boot holds, unstored, until the person says whose account to sign in as.
  const [linkAsk, setLinkAsk] = useState<{
    subject: string | null;
    org: string | null;
    /** The token this browser held when the question was put. */
    held: string | null;
    answer: (ok: boolean) => void;
  } | null>(null);

  // Global keyboard bindings — disabled while focus is in an input.
  useKeyboard(
    {
      "?": () => setHelpOpen(true),
      // Bound only while it has something to close, so it does not take the key (and
      // preventDefault it) from a view or a component that handles Escape itself.
      ...(helpOpen || inspectedKey
        ? {
            Escape: () => {
              if (helpOpen) setHelpOpen(false);
              else inspect(null);
            },
          }
        : {}),
    },
    authed,
  );

  // Browser back/forward: the view from the URL, then the place the entry remembers — the
  // scroll of <main>, the memory in the drawer, the focused element. A move inside one
  // view (the Graph's walks) restores only what the entry remembers, and its address names
  // the page's namespace after a switch made since.
  useEffect(() => {
    if (!authed) return;
    const sync = (e: PopStateEvent) => {
      // The assistant's phone sheet has an entry of its own: leaving it restores nothing
      // (the sheet put the focus back on its launcher), and an overlay closes on Back.
      if (assistantPopstate(e.state)) return;
      const v = pathnameView();
      if (!v) return;
      const changed = v !== useStore.getState().view;
      const place = readPlace(e.state);
      if (!changed && !place) return addressPageNamespace();
      arriveFromHistory(v, place);
      restorePlace(place, changed);
    };
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, [authed, arriveFromHistory]);

  // The assistant panel: its modes follow the window and the sidebar, a navigation closes
  // it at overlay and phone widths, and Ctrl+/ (⌘/) opens and closes it. Wired once the
  // shell has drawn (the elements a modal panel makes inert exist from then on).
  const shellDrawn = !!identity && !!whoami_;
  useEffect(() => {
    if (!shellDrawn) return;
    bootDock();
    document.addEventListener("keydown", onAssistantShortcut);
    return () => document.removeEventListener("keydown", onAssistantShortcut);
  }, [shellDrawn]);
  const dockMounted = useDock((s) => s.mounted);
  // /assistant is the panel itself, in <main>'s place (lib/assistantDock.ts).
  useEffect(() => {
    if (shellDrawn) showExpanded(view === "assistant");
  }, [shellDrawn, view]);
  const expanded = useDock((s) => s.expanded) && view === "assistant";

  // Goto-prefix: `g` then a letter within 1.2s jumps view (vim/Gmail convention).
  useEffect(() => {
    if (!authed) return;
    let armed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const disarm = () => {
      armed = false;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };
    const onKey = (e: KeyboardEvent) => {
      const t = e.target;
      if (t instanceof HTMLElement && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable))
        return;
      if (e.metaKey || e.ctrlKey || e.altKey || shortcutOff(e)) return;
      if (!armed) {
        if (e.key === "g") {
          armed = true;
          timer = setTimeout(disarm, 1200);
        }
        return;
      }
      // Armed — second key picks destination. `k`/`l` both map to "knowledge"
      // (rail label is "Library", internal id is still "knowledge").
      const dest: Record<
        string,
        "memories" | "knowledge" | "sessions" | "setup" | "browse" | "admin:review_queue" | "admin:activity"
      > = {
        m: "memories",
        k: "knowledge",
        l: "knowledge",
        n: "sessions",
        s: "setup",
        d: "browse",
        r: "admin:review_queue",
        a: "admin:activity",
      };
      const v = dest[e.key];
      if (v) {
        e.preventDefault();
        setView(v);
      }
      disarm();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      disarm();
    };
  }, [authed, setView]);

  // Auth bootstrap on mount
  useEffect(() => {
    (async () => {
      // A sign-in link's token, taken first thing; the catch below reads it too.
      let link: ReturnType<typeof takeLinkToken> = null;
      // The token boot asks whoami about; the catch reads it too.
      let sent: string | null = null;
      // whoami's answer for a same-account link's token, which boot then does not ask again.
      let linkAnswer: WhoAmI | null = null;
      // whoami refused the link's token: the gate says so.
      let linkRefused = false;
      try {
        // A login link (federation redirect) carries the token in the #token= fragment: taken
        // and scrubbed before anything can fail. The return to the page its sign-in started on
        // is a chunk of its own, and boot goes on without it.
        link = takeLinkToken();
        const started = takeSignInStarted(link?.nonce ?? null);
        if (takeSsoAsk()) setSsoAsked(true);
        if (link) {
          // setToken strips whitespace; whoami is asked about the same token it stores.
          const token = link.token?.replace(/\s+/g, "") || null;
          link = { ...link, token };
          // A proxy or auth off: the page sends no token of its own, so a link's is dropped.
          if (token && (isProxyMode() || isAuthDisabled())) link = { ...link, token: null };
          // The sign-in this tab started (its nonce came back) skips the checks below.
          else if (token && !started) {
            // Asked about with its own header first; nothing is stored, nor any tab told, before.
            const answer = await whoamiWhenReady(token).catch(() => null);
            // Refused, expired or unanswered: dropped without asking.
            if (!answer?.authenticated) {
              linkRefused = answer?.authenticated === false;
              link = { ...link, token: null };
            }
            // Another account than the one held: the person confirms the one the server named.
            // Boot asks whoami again: the person may read the question longer than it lives.
            else if (!sameAccount(answer, storedToken())) {
              let ok: boolean;
              let held: string | null;
              // Another tab that stores a token while the screen is up changes whose account
              // a confirmation replaces: the question is put again, naming it. A cancel stands.
              do {
                held = storedToken();
                const ask = { subject: answer.subject ?? null, org: answer.org ?? null, held };
                ok = await new Promise<boolean>((resolve) => setLinkAsk({ ...ask, answer: resolve }));
              } while (ok && storedToken() !== held);
              setLinkAsk(null);
              if (!ok) link = { ...link, token: null };
            } else linkAnswer = answer;
          }
          if (link.token) setToken(link.token);
          try {
            (await import("./lib/linkToken")).returnToSignInStart(link.atRoot);
          } catch {
            /* the address stays where the token came back */
          }
        }

        // A link that names a collection as its namespace opens it in the Library instead.
        const initial = collectionLinkView(pathnameView());

        // Auth-disabled mode (LOOM_AUTH_REQUIRED=false): skip TokenGate, synthesise
        // a local identity. The server accepts every /v1/* call without a bearer.
        // The namespace is the one the server serves (LOOM_DEFAULT_NAMESPACE,
        // injected via the boot snippet; "local" when unset) — the saved pick is
        // deliberately NOT consulted. The auth-on path below validates a saved
        // pick against the reachable set; this mode has no JWT claim and no grants
        // to build that set from, and the server bootstraps a loom.namespaces row
        // for the configured namespace alone. Honouring a stale pick (left by
        // auth-on mode, or by a previous default) would point the dashboard at a
        // namespace with no backing row and land writes there — the orphaned-data
        // case the bootstrap exists to prevent. Equivalently: the reachable set
        // here is exactly one name, so validating a pick can only ever return it.
        if (isAuthDisabled()) {
          const authOffNs = defaultNamespace();
          setAuthed(true);
          setWhoami({
            authenticated: true,
            subject: "local",
            org: "dev",
            namespaces: [authOffNs],
            roles: ["PLATFORM_ADMIN"],
            is_service_account: false,
          });
          bootIdentity({ org: "dev", namespace: authOffNs, agent: "loom-ui" });
          // Local mode uses one namespace, but explicit links still choose the
          // view: model onboarding and refreshes must not land on Memories.
          bootView(initial ?? "memories");
          return;
        }

        // Everything that does not depend on whoami starts now — the membership read
        // keyed by the token's own org claim, and the chunk for the view the URL names.
        // whoami still decides the gate below; on a token the server rejects the early
        // read fails quietly and one chunk was fetched for nothing.
        preloadView(initial ?? "memories");
        const early = claimsFromToken(storedToken());
        const earlyMine: Promise<MyNamespaceEntry[] | null> | null = early?.org
          ? sharedMine(early.org).catch(() => null)
          : null;

        sent = storedToken();
        const w = linkAnswer ?? (await whoamiWhenReady());
        // Another tab stored someone's token while whoami answered for this one: that
        // token decides, on a fresh boot.
        if (storedToken() && storedToken() !== sent) {
          reloadPage();
          return;
        }
        // A refused read (here or in another tab) dropped the token while whoami answered.
        const dropped = !!sent && !storedToken();
        if (!w.authenticated || dropped) {
          // A stored token the server no longer accepts is dead — drop it so the next
          // load doesn't retry it, and tell the gate why it is showing instead of a
          // silent first-visit card. A sign-out (here or in another tab) is a plain
          // sign-in. A refused sign-in link says so, and starts no sign-in by itself: it
          // may be the way in while the identity provider is down.
          const out = signedOut();
          const note = sessionEndedNote();
          if (((note || dropped) && !out) || hasToken() || linkRefused) {
            clearToken();
            useStore.getState().setGateReason(link?.token || linkRefused ? "refused" : note || lostReason(sent));
          }
          setAuthed(false);
          return;
        }
        sessionEndedNote();

        setAuthed(true);
        setWhoami(w);
        // Saved pick → the user's own namespace → first real claim → none. Access comes
        // from the JWT `namespaces` claim AND namespaces JOINed via Discover
        // (loom.namespace_grants, not in the JWT), so list_mine is consulted too — else a
        // refresh flips joined-namespace users back. The claim's ORDER is never a
        // preference, and `*` is never a namespace name. A link's `?namespace=` comes
        // before the saved pick, for this tab only.
        const saved = getSavedNamespace();
        const linked = linkedNamespace();
        const claims = w.namespaces || [];
        const effectiveOrg = w.org || "dev";
        // A deep link whose namespace the token's own claims already settle paints its
        // shell and starts its reads now, before the membership list answers; the answer
        // below replaces the namespace only when it differs (a joined namespace the
        // claims do not carry). The bare root waits: its landing view depends on the kinds
        // only list_mine knows. So does a link to a namespace the claims do not name.
        const provisional = initial
          ? provisionalNamespace(initial, linked ?? saved, linked && !claims.includes(linked) ? [] : claims)
          : null;
        if (initial && provisional) {
          bootIdentity({ org: effectiveOrg, namespace: provisional, agent: "loom-ui" });
          bootView(initial);
        }
        let mine: MyNamespaceEntry[] = [];
        try {
          // The one list_mine every asker joins — here the read that started before
          // whoami, when the token's org claim matches the verified one.
          const joined = earlyMine && early?.org === effectiveOrg ? await earlyMine : null;
          mine = joined ?? (await sharedMine(effectiveOrg));
          useStore.getState().setMyNamespaces(mine); // roles for the admin gate
          rememberKinds(mine); // the next visit's deep link knows its kind
        } catch {
          /* fail-quiet: fall back to the JWT claim alone */
        }
        // A link's namespace opens when the account can read it, and is never saved as the
        // pick. One it cannot read leaves the page on the namespace a visit without the
        // link opens, behind a page that says so (`ViewSwitch`). Every address the app writes
        // names its namespace, so a reload is a link too: when the membership read failed, the
        // saved pick still opens (the server answers every read), and any other link's page
        // says its access could not be checked.
        const known = useStore.getState().myNamespaces;
        const readable = linked !== null && (canReadNamespace(linked, claims, known) || (!known && linked === saved));
        // A Library link to a collection is the collection's: one its namespace denies the
        // reader still opens, on the reader's own namespace, and the Library reads the
        // collection only if collection.list names it.
        const collectionLink =
          initial === "knowledge" && isCollectionName(new URLSearchParams(location.search).get("collection"));
        const denied = linked !== null && !readable && !collectionLink ? linked : null;
        const pick = readable ? linked : saved;
        const reach = readable ? [linked] : claims;
        // URL routing: an explicit path (e.g. /memories) always wins — the landing rule decides a
        // view only for the bare root. A deep link keeps its view; its namespace is the
        // default-namespace chain's answer, else the first reachable namespace of the
        // kind the view needs (a code or QA one for Repositories, Graph, Settings, Spaces
        // and the admin views; nothing for a memory view, which renders its own empty
        // state — see `ViewSwitch`). No deep link is redirected to Setup.
        if (initial) {
          const ns = deepLinkNamespace(initial, pick, reach, mine);
          // An empty namespace is the honest "nothing selected" state; every
          // view's ScopeBadge names it. The provisional paint above usually already
          // holds this answer, and an equal identity keeps its object.
          bootIdentity({ org: effectiveOrg, namespace: ns ?? "", agent: "loom-ui" }, denied, readable ? linked : null);
          bootView(initial);
        } else {
          // The bare root: the default-namespace chain (saved pick → own memory namespace →
          // first memory claim) lands on Memories, where the first-run checklist sits. A saved
          // pick of a code or tracker namespace, or a token that reaches only code/tracker
          // namespaces, lands on Repositories with the picker on that namespace; one that
          // reaches only shared collections lands on the Library, and one that reaches nothing
          // else lands on Setup, whose notice says how to get in.
          let target = landingTarget(pick, reach, mine);
          if (target.view === "setup") {
            // Nothing but shared collections, maybe: `collection.list` decides, with the gate's
            // rules (`landingWithCollections`). Loaded on demand: in the shell it takes the index
            // chunk over its budget; a failed load lands on Setup.
            const setup = target;
            target = await import("./lib/collectionPlaces").then(
              (m) => m.landingWithCollections(effectiveOrg, mine),
              () => setup,
            );
          }
          bootIdentity(
            { org: effectiveOrg, namespace: target.namespace ?? "", agent: "loom-ui" },
            denied,
            readable ? linked : null,
          );
          if (target.view === "setup") {
            // Setup's notice says how to get in and opens the picker on request; opened here
            // it would cover that notice's actions.
            bootView("setup");
          } else {
            bootView(target.view ?? (isCollectionName(target.namespace) ? "knowledge" : "memories"));
          }
        }

        // Approval-notification check: anything that flipped pending→active since
        // last visit means an admin approved while the user was gone — toast it.
        try {
          // The list fetched above is the same list — one call.
          const PENDING_KEY = pendingKey(w.subject, effectiveOrg);
          const priorPending: string[] = JSON.parse(localStorage.getItem(PENDING_KEY) || "[]");
          const nowPending = new Set(
            mine
              .filter((m) => m.pending_role || m.status === "pending")
              .map((m) => `${m.namespace}::${m.pending_role || m.role}`),
          );
          for (const key of priorPending) {
            if (nowPending.has(key)) continue;
            const [ns, requestedRole] = key.split("::");
            // Look up the granted_by on the now-active grant. Best
            // effort — if listGrants fails or we're not allowed,
            // surface a generic toast.
            try {
              const r = await listGrants({ org: effectiveOrg, namespace: ns });
              const me = w.subject || "";
              const my = (r.rows || []).find((row) => row.subject === me);
              if (my && my.status === "active" && !my.pending_role) {
                toast(`${my.granted_by} approved your ${requestedRole} request on ${ns}`, "ok");
              }
            } catch {
              toast(`Your ${requestedRole} request on ${ns} was approved`, "ok");
            }
          }
          localStorage.setItem(PENDING_KEY, JSON.stringify(Array.from(nowPending)));
        } catch {
          /* notification check is best-effort */
        }
      } catch (e) {
        // Still unavailable after whoami's retries: the toast says so, and the token stays
        // for the next load. A sign-in link refused on the way says so and leaves no note,
        // so the next load does not start a sign-in by itself (the link may be the way in).
        // A token refused on the way (the early read) says so on the gate, as the try would.
        // Only a token this boot itself lost: an earlier load's note alone (a Reload while
        // the server is away) starts no sign-in by itself.
        const note = sessionEndedNote();
        if (!storedToken() && (link?.token || linkRefused || (note && !signedOut() && sent)))
          useStore.getState().setGateReason(link?.token || linkRefused ? "refused" : note);
        toastError("Auth bootstrap failed", e, { read: true });
        setAuthed(false);
      }
    })();
    // Auth bootstrap runs once per page load; later store changes must not re-run it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (linkAsk) {
    return (
      <Suspense fallback={null}>
        <LinkTokenConfirm
          subject={linkAsk.subject}
          org={linkAsk.org}
          current={claimsFromToken(linkAsk.held)}
          onConfirm={() => linkAsk.answer(true)}
          onCancel={() => linkAsk.answer(false)}
        />
      </Suspense>
    );
  }
  // Unauthed → token gate
  if (!authed && !isProxyMode()) {
    return (
      <>
        <Suspense fallback={null}>
          <TokenGate onLogin={() => location.reload()} reason={gateReason} ssoAsked={ssoAsked} />
        </Suspense>
        <ToastViewport />
        <ConfirmViewport />
      </>
    );
  }
  if (!authed && isProxyMode()) {
    return (
      <main className="min-h-screen grid place-items-center text-sm text-text-muted">
        <h1 className="sr-only">Loom</h1>
        Proxy auth failed — check your gateway session.
      </main>
    );
  }
  if (!identity || !whoami_) {
    return (
      <main className="min-h-screen grid place-items-center text-sm text-text-muted" aria-busy="true">
        <h1 className="sr-only">Loom</h1>
        <Loading label="Loading Loom" />
      </main>
    );
  }

  return (
    <div className="h-screen flex flex-col bg-bg text-text">
      <a
        href={expanded ? "#assistant-panel" : "#main"}
        data-dock-inert=""
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[60] focus:px-3 focus:py-1.5 focus:bg-accent focus:text-on-accent focus:rounded-sm focus:shadow-lg text-xs font-medium"
      >
        Skip to main content
      </a>
      {/* `data-dock-inert`: what a modal assistant panel makes inert (lib/assistantDock.ts);
          the wrappers are `display: contents`, so the layout is the children's. */}
      <div className="flex flex-1 min-h-0">
        <div className="contents" data-dock-inert="">
          <Sidebar />
        </div>
        <div className="flex-1 flex flex-col min-w-0">
          <div className="contents" data-dock-inert="">
            <ReloadBar />
            <Header />
          </div>
          <div className="flex flex-1 min-h-0">
            {/* `relative`: a visually hidden (absolute) label inside a scrolled row is placed
              against main, so it can never stretch the page past the viewport. `isolate`
              keeps the page's own z-indexes under the assistant panel's layer. */}
            <main
              id="main"
              tabIndex={-1}
              data-dock-inert=""
              className="relative isolate flex-1 min-w-0 overflow-y-auto"
              ref={mainRef}
              hidden={expanded}
            >
              <Suspense fallback={null}>
                <ProblemsBanner />
              </Suspense>
              <PendingRequestBanner />
              {/* Keyed by view so each navigation replays the fade-in, and the no-access
                page is a page of its own: leaving it mounts the view afresh, never under
                the hidden old page. `h-full`
                gives a view that wants the page's height (the Explorer's
                three panes) something to resolve against;
                taller views overflow it and scroll <main> as before. */}
              <div key={denied ? "no-access" : view} className="animate-view-in h-full">
                {/* Delayed, so a chunk that arrives quickly never flashes a loader. */}
                <Suspense
                  fallback={
                    <LoadingSkeleton label="Loading page" className="p-6">
                      <SkeletonCard />
                    </LoadingSkeleton>
                  }
                >
                  <ViewSwitch view={view} />
                </Suspense>
              </div>
            </main>
            {dockMounted && (
              <Suspense fallback={null}>
                <AssistantPanel />
              </Suspense>
            )}
          </div>
          <div className="contents" data-dock-inert="">
            <MobileNav />
          </div>
        </div>
      </div>
      {/* Where a navigation went, said once (lib/place.ts `announce`). */}
      <p id="loom-nav-status" role="status" className="sr-only" />
      {/* The assistant's own announcements: an answer started, ended, is ready. Outside the
          panel and its chunk, so it is live from the first paint and never inert. */}
      <p id="loom-assistant-status" role="status" aria-live="polite" aria-atomic="true" className="sr-only" />

      {inspectedKey && inspectorIdentity && !denied && (
        <Suspense fallback={null}>
          <MemoryInspector
            identity={inspectorIdentity}
            memoryKey={inspectedKey}
            onClose={() => inspect(null)}
            onChanged={() => useStore.getState().bumpMemoriesVersion()}
          />
        </Suspense>
      )}

      {/* Mounted only while open, so their chunks
          load on the first open, not with the shell. */}
      {inspectedProfileLabel && (
        <Suspense fallback={null}>
          <ProfileSectionDialog />
        </Suspense>
      )}
      {helpOpen && (
        <Suspense fallback={null}>
          <HelpDialog open={helpOpen} onOpenChange={setHelpOpen} />
        </Suspense>
      )}
      <NetworkProgress />
      <ToastViewport
        pinned={
          sessionExpired ? (
            <Suspense fallback={null}>
              <SessionExpiredBar />
            </Suspense>
          ) : null
        }
      />
      <ConfirmViewport />
    </div>
  );
}

function ViewSwitch({ view }: { view: ReturnType<typeof useStore.getState>["view"] }) {
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  const mine = useStore((s) => s.myNamespaces);
  const denied = useStore((s) => s.deniedNamespace);
  // A link to a namespace the account cannot read shows that, never the view: on the
  // namespace under it the view would read data the link did not ask for.
  if (denied) return <NamespaceNoAccess namespace={denied} />;
  // A view that reads the memory plane never mounts on a code or tracker namespace, or on
  // no namespace — it would fire memory.* reads the token cannot make against a namespace
  // that holds no memories. One empty state, one place, says what the namespace is and
  // where its data lives.
  // The Library still mounts: on such a namespace it reads only the collections it lists.
  if (needsMemoryNamespace(view) && view !== "knowledge" && !isMemoryNamespace(namespace, mine)) {
    return <MemoryScopeMissing view={view} />;
  }
  switch (view) {
    case "memories":
      return <Recent />;
    case "recall":
      return <RecallLab />;
    case "profile":
      return <Profile />;
    case "knowledge":
      return <Documents />; // file unchanged, just relabelled
    case "sharing":
      return <Sharing />;
    case "sessions":
      return <Conversations />; // file unchanged, just relabelled
    case "runs":
      return <Runs />;
    case "graph":
      return <GraphExplorer />;
    case "browse":
      return <Browse />;
    case "code":
      return <CodeView />;
    case "setup":
      return <Setup />;
    case "tasks":
      return <Tasks />; // task inbox board
    case "admin:review_queue":
      return <ReviewQueue />; // Conflicts + Learnings merged
    case "admin:health":
      return <Health />; // Workers + Consolidations merged
    case "admin:guardrails":
      return <Blacklist />; // file unchanged, just relabelled
    case "admin:policies":
      return <Policies />; // history-policy rules
    case "admin:activity":
      return <Audit />; // file unchanged, just relabelled
    case "admin:usage":
      return <Spending />; // file unchanged, just relabelled
    case "admin:agents":
      return <Agents />;
    case "assistant":
      // The assistant panel is this page (layout/AssistantPanel.tsx, expanded).
      return null;
    case "help":
      return <Help />;
    case "admin:users":
      return <AdminUsersView />;
    case "admin:settings":
      return <DeploymentSettings />;
    default:
      return <Recent />;
  }
}
