import { useEffect, useId, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { IconButton } from "../components/IconButton";
import { Tip } from "../components/ui/Tooltip";
import { useStore, viewHref, type AdminBadges, type View } from "../lib/state";
import { countConflicts, countNewAgents, type MyNamespaceEntry, type WhoAmI } from "../api";
import { readSession, useAfterPaint, writeSession } from "../lib/afterPaint";
import { sharedBudgetStatus, sharedHasAutoResolved } from "../lib/sharedReads";
import { hasOrgAdminClaim, hasPlatformAdminClaim, isNamespaceAdmin } from "../lib/role";
import { roleOnNamespace } from "../lib/namespaceRole";
import { canCall } from "../lib/canCall";
import { isMemoryNamespace } from "../lib/namespace";
import { LoomBrand } from "./LoomBrand";

// How long a remembered badge answer is shown before the idle refresh replaces it — one
// poll interval.
const BADGE_SESSION_TTL_MS = 60_000;
// The agents badge reads every fifth minute tick
const AGENTS_EVERY_N_TICKS = 5;

export interface NavItem {
  id: View;
  label: string;
  icon: IconName;
  /** What the page is scoped by — the namespace picked in the header, the whole org
   *  (Repositories, Tasks), or the whole deployment (Deployment settings). Read out in the
   *  item's tooltip so a namespace switch never surprises. */
  scope?: "namespace" | "org" | "deployment";
  /** The tooltip's scope read-out, where the scope alone does not say it. */
  hint?: string;
  badge?: number;
  // Tone steers the dot's color so a warning (over-cap) reads
  // differently from a neutral count (review queue). Number >= 999
  // collapses to a dot rather than a numeric label.
  badgeTone?: "default" | "warn" | "accent";
}

// The phone's More dialog uses the same destinations so route labels stay in sync.
interface NavGroup {
  label: string;
  items: NavItem[];
}

export const RAIL_GROUPS: NavGroup[] = [
  {
    label: "Context",
    items: [
      { id: "memories", label: "Memories", icon: "clock", scope: "namespace" },
      {
        id: "knowledge",
        label: "Library",
        icon: "document",
        scope: "namespace",
        hint: "in the namespace picked above, or a collection you read",
      },
      { id: "profile", label: "Profile", icon: "cards", scope: "namespace" },
    ],
  },
  {
    label: "Agent activity",
    items: [
      { id: "sessions", label: "Conversations", icon: "chat", scope: "namespace" },
      { id: "runs", label: "Runs", icon: "list-numbered", scope: "namespace" },
      { id: "tasks", label: "Tasks", icon: "email", scope: "org" },
    ],
  },
  {
    label: "Sources",
    items: [{ id: "code", label: "Repositories", icon: "code", scope: "org" }],
  },
];
export const MAIN: NavItem[] = RAIL_GROUPS.flatMap((g) => g.items);
export const SETTINGS_ITEM: NavItem = { id: "setup", label: "Settings", icon: "gear", scope: "namespace" };
export const DOCS_ITEM: NavItem = { id: "help", label: "Documentation", icon: "book" };

// Admin drawer items. Review queue is HIDDEN by default — it's a
// usually-empty surface (conflicts/learnings only appear on
// multi-writer namespaces) and a permanent rail entry advertising
// emptiness is noise. The route stays resolvable for deep links; when there's actually
// something to review the entry slides in automatically via the badge logic below.
//
// Badges are layered on at render time by `adminItems` so the
// labels can come straight from the const while count / warning
// state stays reactive.
// The drawer reads as three workflows — Review (decide something), Observe (see what
// happened), Access (who may) — rather than a flat list. ADMIN_GROUPS is the rendering
// order; ADMIN_BASE stays the flat list adminItems and the tests read. Guardrails (the
// blacklist) is in the drawer so it is reachable from somewhere other than Settings.
export const ADMIN_GROUPS: NavGroup[] = [
  {
    label: "Review",
    items: [
      { id: "admin:guardrails", label: "Guardrails", icon: "line-in-circle", scope: "namespace" },
      // Policies: history rules over what an agent may do next — forbid /
      // require an action based on the caller's own recent trace.
      { id: "admin:policies", label: "Policies", icon: "secure", scope: "namespace" },
    ],
  },
  {
    label: "Observe",
    items: [
      { id: "admin:health", label: "Health", icon: "activity", scope: "namespace" },
      { id: "admin:activity", label: "Audit log", icon: "history", scope: "namespace" },
      // Flow (route id "sharing" — stable deep links): a READ-ONLY observability
      // view — where memories flow (writes/recalls, cross-namespace reach,
      // per-tool producer/consumer activity, memberships).
      { id: "sharing", label: "Flow", icon: "share-network", scope: "namespace" },
      { id: "admin:usage", label: "Usage", icon: "dollar", scope: "namespace" },
      { id: "admin:agents", label: "Agents", icon: "plug", scope: "namespace" },
    ],
  },
];
export const ADMIN_BASE: NavItem[] = ADMIN_GROUPS.flatMap((g) => g.items);

/** A view that lives in the admin drawer: the `admin:` ids and the routes
 * filed there under a stable non-prefixed id (Flow). The drawer opens on a deep link to
 *  any of them. */
export function isDrawerView(v: View): boolean {
  return (
    v.startsWith("admin:") ||
    ADMIN_BASE.some((i) => i.id === v) ||
    [...ADMIN_OPERATOR_ONLY, ...ADMIN_PLATFORM_ONLY].some((i) => i.id === v)
  );
}

// Operator-only items — shown only when the caller's JWT carries
// PLATFORM_ADMIN / ORG_ADMIN. The Users view is the most-load-bearing
// of these (you can't manage who has access without seeing the list).
export const ADMIN_OPERATOR_ONLY: NavItem[] = [{ id: "admin:users", label: "Users", icon: "users", scope: "org" }];
/** Platform-admin-only items: the deployment's own settings, which apply to every org and
 *  which `/v1/admin/settings.*` serves to a platform admin alone. */
export const ADMIN_PLATFORM_ONLY: NavItem[] = [
  { id: "admin:settings", label: "Deployment settings", icon: "settings", scope: "deployment" },
];
/** The drawer's Access group: the operator entries shown to platform / org admins. */
const ACCESS_GROUP: NavGroup = { label: "Access", items: ADMIN_OPERATOR_ONLY };
const DEPLOYMENT_GROUP: NavGroup = { label: "Deployment", items: ADMIN_PLATFORM_ONLY };

/** The operator entries the caller's claims open: Users for an org or platform admin,
 *  Deployment settings for a platform admin. */
export function operatorItems(whoami: WhoAmI | null): NavItem[] {
  return [
    ...(hasOrgAdminClaim(whoami) ? ADMIN_OPERATOR_ONLY : []),
    ...(hasPlatformAdminClaim(whoami) ? ADMIN_PLATFORM_ONLY : []),
  ];
}

/** The drawer items a caller may see, in their groups — Review queue leads Review while
 *  there is something to review; Users in the Access group only for platform / org admins,
 *  the Deployment group only for platform admins. */
export function groupAdminItems(visible: NavItem[]): NavGroup[] {
  const byId = new Map(visible.map((i) => [i.id, i] as const));
  const out: NavGroup[] = [];
  const review = byId.get("admin:review_queue");
  for (const g of [...ADMIN_GROUPS, ACCESS_GROUP, DEPLOYMENT_GROUP]) {
    const items = g.items.map((i) => byId.get(i.id)).filter((i): i is NavItem => !!i);
    if (g.label === "Review" && review) items.unshift(review);
    if (items.length > 0) out.push({ label: g.label, items });
  }
  return out;
}

/** The Admin entries a caller sees, in the drawer's order, with their badges. None unless
 *  the caller admins the namespace (an admin grant from list_mine, or an admin claim), so a
 *  viewer never sees an entry with a 403 behind it. Review queue leads while there is
 *  something to review; the operator entries follow the caller's claims. The sidebar and
 *  the phone's More sheet both list these. */
export function adminItems(
  whoami: WhoAmI | null,
  mine: MyNamespaceEntry[] | null,
  namespace: string,
  badges: AdminBadges | null,
): NavItem[] {
  if (!isNamespaceAdmin(whoami, mine, namespace)) return [];
  // Over cap on Usage is a warning dot; Agents counts the clients new in the last 24 h.
  const items = [...ADMIN_BASE, ...operatorItems(whoami)].map((item): NavItem => {
    if (item.id === "admin:usage" && badges?.overCap) return { ...item, badge: 999, badgeTone: "warn" };
    if (item.id === "admin:agents" && badges?.newAgents)
      return { ...item, badge: badges.newAgents, badgeTone: "accent" };
    return item;
  });
  const open = !!badges?.conflicts;
  if (!open && !badges?.autoResolved) return items;
  // Open conflicts warn with their count; recent auto-resolutions alone use the
  // Auto-resolved tab's icon, no number.
  const review: NavItem = {
    id: "admin:review_queue",
    label: "Review queue",
    icon: open ? "warning" : "flash",
    badge: badges?.conflicts || undefined,
  };
  return [review, ...items];
}

export function Sidebar() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const identity = useStore((s) => s.identity);
  const collapsed = useStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useStore((s) => s.toggleSidebar);
  const [adminOpen, setAdminOpen] = useState(isDrawerView(view));
  const adminDrawerId = useId();
  useEffect(() => {
    if (isDrawerView(view)) setAdminOpen(true);
  }, [view]);
  // Lean notification surface: badge admin items that have actionable state right now. The
  // answer is kept in the store, where the phone's More sheet reads it too.
  const badges = useStore((s) => s.adminBadges);
  const agentsTick = useRef(0);

  const whoami = useStore((s) => s.whoami);
  const myNamespaces = useStore((s) => s.myNamespaces);
  // The drawer may stay fail-open (a transient list_mine failure must not hide an admin's
  // controls, and deep links still resolve), but the polls that badge it need a POSITIVE
  // admin signal — an admin claim or an active admin grant on this namespace.
  // `isNamespaceAdmin` is true when list_mine failed ("unknown"), and a token-only
  // principal on a jwt-only namespace has no user row, so its list_mine is empty or
  // fails; polling on "unknown" would fire conflicts.list + cost.budget_status every 30 s
  // into AUTH_INSUFFICIENT. Unknown does not poll.
  const roleHere = roleOnNamespace(whoami, myNamespaces, identity?.namespace ?? "");
  // The badges count conflicts, budget and agents of the memory plane; a code index or a
  // tracker namespace has none, so its admin is not polled for them either.
  const pollsBadges = roleHere === "admin" && isMemoryNamespace(identity?.namespace ?? "", myNamespaces);

  const visibleAdmin = adminItems(whoami, myNamespaces, identity?.namespace ?? "", badges);
  const conflicts = visibleAdmin.find((item) => item.id === "admin:review_queue")?.badge;

  // Fetch conflict count + recent auto-resolutions + budget + agents activity on identity
  // change and refresh every 60s. All four are fail-quiet — a missing
  // backend op just leaves its badge hidden. Only for a known admin of this
  // namespace: otherwise a viewer-only token draws AUTH_INSUFFICIENT 403s on every page
  // load, and a token-only principal through the fail-open gate. Each call also passes
  // `canCall` so the rule lives in one place. The reads leave AFTER the page's
  // first paint, on idle — a badge number is not what the person is waiting for — and the
  // last answer is remembered for the session, so a full reload shows the badges at once
  // from it and refreshes them behind.
  const badgeKey = identity ? `badges:${identity.org}/${identity.namespace}` : "";
  // A badge belongs to one namespace: on a switch, show the new namespace's remembered
  // answer or nothing — never the previous namespace's numbers — and make its first read
  // ask agents.connected, so no tick carries the old count into the new cache key.
  useEffect(() => {
    if (!badgeKey) return;
    agentsTick.current = 0;
    useStore.setState({ adminBadges: readSession<AdminBadges>(badgeKey, BADGE_SESSION_TTL_MS) });
  }, [badgeKey]);
  useAfterPaint(() => {
    if (!identity || !pollsBadges) return;
    let cancelled = false;
    const fetch_ = async () => {
      const tasks: Promise<void>[] = [];
      const next: AdminBadges = { conflicts: null, autoResolved: false, overCap: false, newAgents: 0 };
      if (canCall("conflicts.list", roleHere, whoami)) {
        tasks.push(
          // The badge asks for the number, not 100 rows with their values (33 MB per tick
          // on a large namespace).
          countConflicts({ org: identity.org, namespace: identity.namespace })
            .then((n) => {
              next.conflicts = n;
            })
            .catch(() => {
              /* hide */
            }),
        );
      }
      // The Review queue shows the Auto-resolved tab only to a role that reaches both.
      if (canCall("memory.auto_retirements", roleHere, whoami) && canCall("audit.list", roleHere, whoami)) {
        tasks.push(
          // Whether the Auto-resolved tab has a row (`hasAutoResolved` says what it reads).
          // Shared with the Review queue page, which asks the same question.
          sharedHasAutoResolved({ org: identity.org, namespace: identity.namespace })
            .then((found) => {
              next.autoResolved = found;
            })
            .catch(() => {
              /* hide */
            }),
        );
      }
      if (canCall("cost.budget_status", roleHere, whoami)) {
        tasks.push(
          sharedBudgetStatus({ org: identity.org, namespace: identity.namespace })
            .then((s) => {
              next.overCap = !!s.over_cap;
            })
            .catch(() => {
              /* hide */
            }),
        );
      }
      // The agents badge is a number that changes a few times a day. It asks the server
      // for the count (one aggregate, no rows) and only every fifth tick — five minutes —
      // where every other badge keeps the minute. 16,528 row listings in seven days for
      // it.
      if (canCall("agents.connected", roleHere, whoami) && agentsTick.current % AGENTS_EVERY_N_TICKS === 0) {
        tasks.push(
          // namespace in the body: a scoped token is refused without one
          countNewAgents({ org: identity.org, namespace: identity.namespace, window_hours: 24 })
            .then((count) => {
              next.newAgents = count;
            })
            .catch(() => {
              /* hide */
            }),
        );
      } else {
        next.newAgents = useStore.getState().adminBadges?.newAgents ?? 0;
      }
      agentsTick.current += 1;
      // One answer once every read settles: a failed read hides its badge.
      await Promise.allSettled(tasks);
      if (cancelled) return;
      useStore.setState({ adminBadges: next });
      writeSession(badgeKey, next);
    };
    void fetch_();
    // 60 s and only while the tab is visible (hidden tabs read nothing).
    let missed = false;
    const tick = (): void => {
      if (document.hidden) {
        missed = true;
        return;
      }
      void fetch_();
    };
    const onVisible = (): void => {
      if (!document.hidden && missed) {
        missed = false;
        void fetch_();
      }
    };
    const id = setInterval(tick, 60_000);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [identity, pollsBadges, roleHere, whoami, badgeKey]);

  return (
    <aside
      className={`workspace-rail hidden md:flex shrink-0 border-r border-border flex-col ${collapsed ? "w-[60px]" : "w-[244px]"}`}
    >
      {/* Brand + collapse — brand is an anchor: cmd-click opens Memories in a new tab. The
          toggle is one button in both states, so it keeps the focus when pressed. */}
      <div
        className={`workspace-rail-brand flex items-center justify-between ${collapsed ? "flex-col gap-2 px-2 py-3" : "min-h-14 px-4"}`}
      >
        <a
          href={viewHref("memories")}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
            e.preventDefault();
            setView("memories");
          }}
          className="rounded-sm no-underline hover:no-underline"
          aria-label="Loom — go to Memories"
        >
          <LoomBrand compact={collapsed} />
        </a>
        <IconButton
          icon={collapsed ? "slide-in" : "slide-out"}
          size="sm"
          label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
          onClick={toggleSidebar}
        />
      </div>

      {/* Main nav — anchors so cmd-click opens in a new tab. Padded, not margined, so the
          scroll box leaves room for the first and last items' focus ring. */}
      <nav className="flex-1 px-3 pt-3 pb-2 overflow-y-auto" aria-label="Sections">
        {RAIL_GROUPS.map((g, gi) => (
          <div
            key={g.label}
            className={`${NAV_LIST} ${gi === 0 ? "" : collapsed ? "pt-2 mt-2 border-t border-border" : "pt-4"}`}
          >
            {!collapsed && <GroupLabel>{g.label}</GroupLabel>}
            {g.items.map((item) => (
              <NavBtn
                key={item.id}
                item={item}
                active={view === item.id || (item.id === "memories" && view === "recall")}
                collapsed={collapsed}
                onActivate={() => setView(item.id)}
              />
            ))}
          </div>
        ))}

        {!collapsed && visibleAdmin.length > 0 && (
          <div className="pt-4">
            <button
              type="button"
              onClick={() => setAdminOpen((v) => !v)}
              className={`${NAV_ITEM} w-full justify-between`}
              aria-expanded={adminOpen}
              aria-controls={adminOpen ? adminDrawerId : undefined}
            >
              <span className={`flex items-center ${NAV_GAP}`}>
                <span className={NAV_ICON}>
                  <Icon name="secure" size="sm" aria-hidden="true" />
                </span>
                Admin
              </span>
              <span className="flex items-center gap-2">
                {!!conflicts && !adminOpen && (
                  <span>
                    <span className="sr-only">, {conflicts} open conflicts</span>
                    <span aria-hidden="true">
                      <NavBadge count={conflicts} tone="warn" collapsed={false} />
                    </span>
                  </span>
                )}
                <Icon
                  name="chevron-down"
                  size="sm"
                  aria-hidden="true"
                  className={`transition-transform ${adminOpen ? "rotate-180" : ""}`}
                />
              </span>
            </button>

            {adminOpen && (
              <div id={adminDrawerId} className="ml-1 pl-3 border-l border-border mt-0.5">
                {groupAdminItems(visibleAdmin).map((g, gi) => (
                  <div key={g.label} className={`${NAV_LIST} ${gi > 0 ? "pt-2" : ""}`}>
                    <GroupLabel>{g.label}</GroupLabel>
                    {g.items.map((item) => (
                      <NavBtn
                        key={item.id}
                        item={item}
                        active={view === item.id}
                        collapsed={false}
                        onActivate={() => setView(item.id)}
                      />
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Collapsed rail: admin items get a thin separator + icons so
            they don't disappear when the user collapses the sidebar.
            No dropdown — every icon is one click, tooltip on hover. */}
        {collapsed && visibleAdmin.length > 0 && (
          <div className={`pt-3 mt-2 border-t border-border ${NAV_LIST}`}>
            {visibleAdmin.map((item) => (
              <NavBtn
                key={item.id}
                item={item}
                active={view === item.id}
                collapsed={true}
                onActivate={() => setView(item.id)}
              />
            ))}
          </div>
        )}
      </nav>

      <nav aria-label="Workspace" className={`border-t border-border ${NAV_LIST} ${collapsed ? "p-2" : "p-3"}`}>
        <NavBtn item={DOCS_ITEM} active={view === "help"} collapsed={collapsed} onActivate={() => setView("help")} />
        <NavBtn
          item={SETTINGS_ITEM}
          active={view === "setup" || view === "connected-accounts"}
          collapsed={collapsed}
          onActivate={() => setView("setup")}
        />
      </nav>
    </aside>
  );
}

// Click UI's SidebarNavigationItem renders a div with onClick (no href, no cmd-click), so
// the rail keeps its anchors and draws them with that component's own variables: 400
// 14 px, 4.5 / 12 / 16 px padding, a 16 px muted icon 12 px from the label, 4 px corners,
// the hover and selected fills. Click UI gives the item no focus ring; Loom's is added.
const NAV_LIST = "flex flex-col gap-(--click-sidebar-navigation-item-global-gap)";
const NAV_GAP = "gap-(--click-sidebar-navigation-item-default-space-gap)";
const NAV_ITEM = [
  "workspace-nav-item relative flex items-center min-w-0 rounded-(--click-sidebar-navigation-item-radii-all)",
  "min-h-9 py-(--click-sidebar-navigation-item-default-space-y) px-3 pointer-coarse:min-h-11",
  NAV_GAP,
  "[font:var(--click-sidebar-navigation-item-typography-default)]",
  "no-underline hover:no-underline",
].join(" ");
const NAV_ICON = "inline-flex shrink-0 text-(--click-sidebar-main-navigation-item-color-icon-default)";
const NAV_TITLE =
  "pl-3 pb-1 select-none [font:var(--click-sidebar-navigation-title-typography-default)] text-text-muted";

function GroupLabel({ children }: { children: string }) {
  return <div className={NAV_TITLE}>{children}</div>;
}

/** The scope read-out for an item's tooltip / title. */
export function scopeHint(item: NavItem): string {
  if (item.hint) return item.hint;
  if (item.scope === "org") return "across every namespace";
  if (item.scope === "deployment") return "for every org on this Loom";
  if (item.scope === "namespace") return "in the namespace picked above";
  return "";
}

function NavBtn({
  item,
  active,
  onActivate,
  collapsed,
}: {
  item: NavItem;
  active: boolean;
  onActivate: () => void;
  collapsed: boolean;
}) {
  const hint = scopeHint(item);
  const hintId = useId();
  // Anchor (not button) so cmd/ctrl-click opens the view in a new tab,
  // middle-click works, and "Copy link" gives a usable URL.
  const anchor = (
    <a
      href={viewHref(item.id)}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        onActivate();
      }}
      className={`${NAV_ITEM} ${collapsed ? "justify-center px-2!" : "justify-between"}`}
      aria-label={collapsed ? collapsedName(item) : undefined}
      aria-current={active ? "page" : undefined}
      aria-describedby={hint ? hintId : undefined}
    >
      {/* The scope hint is the link's description, not part of its name. */}
      {hint && (
        <span id={hintId} hidden>
          {hint}
        </span>
      )}
      <span className={`flex items-center min-w-0 ${NAV_GAP}`}>
        <span className={NAV_ICON}>
          <Icon name={item.icon} size="sm" aria-hidden="true" />
        </span>
        {!collapsed && <span className="truncate">{item.label}</span>}
      </span>
      {item.badge != null && item.badge > 0 && (
        <NavBadge count={item.badge} tone={item.badgeTone ?? "default"} collapsed={collapsed} />
      )}
    </a>
  );
  return (
    <Tip content={item.label} side="right" disabled={!collapsed}>
      {anchor}
    </Tip>
  );
}

/** A collapsed item's name: its label, and its badge's count, which the name would
 *  otherwise hide. */
function collapsedName(item: NavItem): string {
  if (!item.badge) return item.label;
  return item.badge >= 999 ? `${item.label}, attention required` : `${item.label}, ${item.badge}`;
}

/** A nav entry's count (Click UI's small solid badge), or a dot at 999+ for "attention". */
export function NavBadge({
  count,
  tone,
  collapsed,
}: {
  count: number;
  tone: "default" | "warn" | "accent";
  collapsed: boolean;
}) {
  const pin = collapsed ? "absolute -top-0.5 -right-0.5" : "";
  if (count >= 999) {
    return (
      <span
        role="img"
        aria-label="Attention required"
        className={`shrink-0 w-2 h-2 rounded-full ${tone === "warn" ? "bg-warn" : "bg-accent"} ${pin}`}
      />
    );
  }
  return (
    <Badge
      text={String(count)}
      type="solid"
      size="sm"
      state={tone === "warn" ? "warning" : "neutral"}
      className={`shrink-0 tabular ${pin}`}
    />
  );
}
