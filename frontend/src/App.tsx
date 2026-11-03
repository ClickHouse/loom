import { Suspense, lazy, useEffect, useMemo, useRef } from "react";
import { flushView, installVitals, startView } from "./lib/vitals";
import { ReloadBar } from "./components/ReloadBar";
import { invalidateShared, sharedMine, sharedHealth } from "./lib/sharedReads";
import { SkeletonCard } from "./components/Skeleton";
import { Loading, LoadingSkeleton } from "./components/ui";
import {
  whoami,
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
  signedOutJustNow,
  storedToken,
  ApiError,
  type MyNamespaceEntry,
  type WhoAmI,
} from "./api";
import {
  canReadNamespace,
  deepLinkNamespace,
  isMemoryNamespace,
  landingTarget,
  needsMemoryNamespace,
} from "./lib/namespace";
import { claimsFromToken, provisionalNamespace, rememberKinds, takeLinkToken } from "./lib/boot";
import { pendingKey } from "./lib/pendingKey";
import { preloadView } from "./views/registry";
import { MemoryScopeMissing } from "./components/MemoryScopeMissing";
import { useStore, pathnameView, linkedNamespace, addressPageNamespace } from "./lib/state";
import { arriveAt, readPlace, restorePlace } from "./lib/place";
import { useKeyboard } from "./lib/hooks";
import { NetworkProgress } from "./components/NetworkProgress";
import { Sidebar } from "./layout/Sidebar";
import { Header } from "./layout/Header";
import { MobileNav } from "./layout/MobileNav";
import { ToastViewport, toast, toastError } from "./components/Toast";
import { ConfirmViewport } from "./components/ConfirmDialog";
import { PendingRequestBanner } from "./components/PendingRequestBanner";
import { ProblemsBanner } from "./components/ProblemsBanner";
import { shortcutOff } from "./lib/shortcuts";
// Eager, not a chunk: it shows on a long-lived tab, the one most likely to predate a deploy
// that removed its chunk, and a stale-chunk reload would lose the page it keeps.
import { SessionExpiredBar } from "./components/SessionExpiredBar";
import { reloadPage } from "./lib/navigation";

// This tab's session ended on a refused token: a reload's sign-in gate says so, and with SSO
// starts the sign-in by itself, though the token is already gone.
const SESSION_ENDED = "loom_session_ended_v1";

function noteSessionEnded(): void {
  try {
    sessionStorage.setItem(SESSION_ENDED, "1");
  } catch {
    /* no session storage: a reload shows the first-visit gate */
  }
}

/** Whether this tab's session ended on a refused token; reading it forgets it. */
function takeSessionEnded(): boolean {
  try {
    const ended = sessionStorage.getItem(SESSION_ENDED) === "1";
    sessionStorage.removeItem(SESSION_ENDED);
    return ended;
  } catch {
    return false;
  }
}

/** The session ended (the server refused the token, or another tab dropped it). While the
 *  app boots there is no page to keep: the sign-in gate shows, saying why. Mid-session a
 *  card says so over the page, which stays as it is. */
function endSession(): void {
  clearToken();
  // A single-user install has no sign-in: its next call goes without the stale token.
  if (isAuthDisabled()) return;
  noteSessionEnded();
  const s = useStore.getState();
  if (!s.whoami) {
    s.setGateReason("expired");
    s.setAuthed(false);
    return;
  }
  s.setSessionExpired(true);
}

onSignInRejected(endSession);

/** whoami, asked again while the replica answers 503 (its signing keys still loading, the
 *  token may be fine): up to three more times, a second apart, before boot gives up. */
async function whoamiWhenReady(): Promise<WhoAmI> {
  for (let attempt = 1; ; attempt++) {
    try {
      return await whoami();
    } catch (e) {
      if (attempt >= 4 || !(e instanceof ApiError) || e.status !== 503) throw e;
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
}

/** Another tab of this origin stored a token (a sign-in there) or dropped it. Dropped by a
 *  sign-out: this tab reloads to a plain sign-in. Dropped by a refusal: this tab's session
 *  ended too. Stored: once the server confirms it is the same person, this page carries on
 *  with their identity and roles read again; anyone else gets a fresh page, so nothing the
 *  last person read stays on screen. */
async function tokenChangedElsewhere(token: string | null): Promise<void> {
  const s = useStore.getState();
  const before = s.whoami;
  if (!before) return;
  if (token === null) {
    if (signedOutJustNow()) reloadPage();
    else if (!s.sessionExpired) endSession();
    return;
  }
  let now: WhoAmI;
  try {
    now = await whoamiWhenReady();
  } catch {
    // Unconfirmed: a healthy page stays as it is, an ended one keeps its card.
    return;
  }
  // Another change landed while whoami answered: that one decides.
  if (storedToken() !== token) return;
  if (!now.authenticated || !before.subject || now.subject !== before.subject || now.org !== before.org) {
    reloadPage();
    return;
  }
  const wasExpired = useStore.getState().sessionExpired;
  useStore.getState().setWhoami(now);
  useStore.getState().setSessionExpired(false);
  takeSessionEnded();
  if (wasExpired) toast("Signed in again. Retry what failed while the session was away.", "ok");
  try {
    invalidateShared();
    useStore.getState().setMyNamespaces(await sharedMine(now.org ?? ""));
  } catch {
    /* the roles read at boot stay; a reload reads them again */
  }
}

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
const Collections = lazy(() => import("./views/admin/Collections").then((m) => ({ default: m.Collections })));
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

const NamespaceNoAccess = lazy(() =>
  import("./components/NamespaceNoAccess").then((m) => ({ default: m.NamespaceNoAccess })),
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
    arriveAt(useStore.getState().navNote);
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
  // Override agent with the writer's identity (captured when the list-view
  // card was clicked) so strict-tuple memory.get doesn't 404 on cross-agent
  // rows. Memoised: a fresh object every render would replay the inspector's seven reads
  // on any App re-render.
  const inspectorIdentity = useMemo(
    () => (identity ? (inspectedAgent ? { ...identity, agent: inspectedAgent } : identity) : null),
    [identity, inspectedAgent],
  );
  const inspect = useStore((s) => s.inspect);
  const setAuthed = useStore((s) => s.setAuthed);
  const gateReason = useStore((s) => s.gateReason);
  const sessionExpired = useStore((s) => s.sessionExpired);
  // A sign-in or a dropped token in another tab reaches this one (a proxy's header and an
  // auth-off install keep no token).
  useEffect(() => {
    if (!authed || isProxyMode() || isAuthDisabled()) return;
    return onTokenStored((token) => void tokenChangedElsewhere(token));
  }, [authed]);
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
      try {
        // A login link (federation redirect) carries the token in the #token= fragment: taken
        // and scrubbed before anything can fail. The return to the page its sign-in started on
        // is a chunk of its own, and boot goes on without it.
        const link = takeLinkToken();
        if (link) {
          if (link.token) setToken(link.token);
          try {
            (await import("./lib/linkToken")).returnToSignInStart(link.atRoot);
          } catch {
            /* the address stays where the token came back */
          }
        }

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
          bootView(pathnameView() ?? "memories");
          return;
        }

        // Everything that does not depend on whoami starts now — the membership read
        // keyed by the token's own org claim, and the chunk for the view the URL names.
        // whoami still decides the gate below; on a token the server rejects the early
        // read fails quietly and one chunk was fetched for nothing.
        const initial = pathnameView();
        preloadView(initial ?? "setup");
        const early = claimsFromToken(storedToken());
        const earlyMine: Promise<MyNamespaceEntry[] | null> | null = early?.org
          ? sharedMine(early.org).catch(() => null)
          : null;

        const sent = storedToken();
        const w = await whoamiWhenReady();
        // Another tab stored someone's token while whoami answered for this one: that
        // token decides, on a fresh boot.
        if (sent && storedToken() && storedToken() !== sent) {
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
          const signedOut = signedOutJustNow();
          const ended = takeSessionEnded() && !signedOut;
          if (ended || hasToken() || (dropped && !signedOut)) {
            clearToken();
            useStore.getState().setGateReason(link?.token && link.token === sent ? "refused" : "expired");
          }
          setAuthed(false);
          return;
        }
        takeSessionEnded();

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
        const denied = linked !== null && !readable ? linked : null;
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
          // first memory claim) keeps its default view, Setup — that's where first-run
          // onboarding and the actions live. A saved pick of a code or tracker namespace,
          // or a token that reaches only code/tracker namespaces, lands on Repositories
          // with the picker on that namespace; only a token that reaches nothing lands on
          // Setup, whose notice says how to get in.
          const target = landingTarget(pick, reach, mine);
          bootIdentity(
            { org: effectiveOrg, namespace: target.namespace ?? "", agent: "loom-ui" },
            denied,
            readable ? linked : null,
          );
          if (target.namespace === null) {
            // Setup's notice says how to get in and opens the picker on request; opened here
            // it would cover that notice's actions.
            bootView("setup");
            toast(
              "Your token can reach no namespace yet — join one from Repositories or Spaces (namespace picker → Browse and join)",
              "error",
            );
          } else {
            bootView(target.view ?? "setup");
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
        // for the next load.
        toastError("Auth bootstrap failed", e);
        setAuthed(false);
      }
    })();
    // Auth bootstrap runs once per page load; later store changes must not re-run it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Unauthed → token gate
  if (!authed && !isProxyMode()) {
    return (
      <>
        <Suspense fallback={null}>
          <TokenGate onLogin={() => location.reload()} reason={gateReason} />
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
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[60] focus:px-3 focus:py-1.5 focus:bg-accent focus:text-on-accent focus:rounded-sm focus:shadow-lg text-xs font-medium"
      >
        Skip to main content
      </a>
      <div className="flex flex-1 min-h-0">
        <Sidebar />
        <div className="flex-1 flex flex-col min-w-0">
          <ReloadBar />
          <Header />
          {/* `relative`: a visually hidden (absolute) label inside a scrolled row is placed
              against main, so it can never stretch the page past the viewport. */}
          <main id="main" tabIndex={-1} className="relative flex-1 overflow-y-auto" ref={mainRef}>
            <ProblemsBanner />
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
          <MobileNav />
        </div>
      </div>
      {/* Where a navigation went, said once (lib/place.ts `announce`). */}
      <p id="loom-nav-status" role="status" className="sr-only" />

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
      <ToastViewport pinned={sessionExpired ? <SessionExpiredBar /> : null} />
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
  if (needsMemoryNamespace(view) && !isMemoryNamespace(namespace, mine)) {
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
    case "admin:collections":
      return <Collections />;
    case "admin:users":
      return <AdminUsersView />;
    case "admin:settings":
      return <DeploymentSettings />;
    default:
      return <Recent />;
  }
}
