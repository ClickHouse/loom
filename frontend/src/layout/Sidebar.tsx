import { useEffect, useId, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { IconButton } from "../components/IconButton";
import { Tip } from "../components/ui/Tooltip";
import { useStore, viewHref, type View } from "../lib/state";
import { countConflicts, countNewAgents, listProposals, type WhoAmI } from "../api";
import { readSession, useAfterPaint, writeSession } from "../lib/afterPaint";
import { sharedBudgetStatus, sharedHasAutoResolved } from "../lib/sharedReads";
import { hasOrgAdminClaim, hasPlatformAdminClaim, isNamespaceAdmin } from "../lib/role";
import { roleOnNamespace } from "../lib/namespaceRole";
import { canCall } from "../lib/canCall";
import { isMemoryNamespace } from "../lib/namespace";

// How long a remembered badge answer is shown before the idle refresh replaces it — one
// poll interval.
const BADGE_SESSION_TTL_MS = 60_000;
// The agents badge reads every fifth minute tick
const AGENTS_EVERY_N_TICKS = 5;

// Loom mark inlined so currentColor inherits from the sidebar's text color.
function LoomMark({ size = 32 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 40 40"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="shrink-0 text-accent"
      aria-hidden="true"
    >
      {/* spokes */}
      <line x1="20" y1="20" x2="20" y2="6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
      <line x1="20" y1="20" x2="34" y2="20" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
      <line x1="20" y1="20" x2="20" y2="34" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
      <line x1="20" y1="20" x2="6" y2="20" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
      {/* peripheral tool nodes */}
      <circle cx="20" cy="6" r="3" fill="currentColor" />
      <circle cx="34" cy="20" r="3" fill="currentColor" />
      <circle cx="20" cy="34" r="3" fill="currentColor" />
      <circle cx="6" cy="20" r="3" fill="currentColor" />
      {/* center memory node — larger, anchoring */}
      <circle cx="20" cy="20" r="6" fill="currentColor" />
      <circle cx="20" cy="20" r="2.4" fill="var(--color-surface)" />
    </svg>
  );
}

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

// The rail is three labelled groups around the questions a user brings — what does my AI
// know (Memory), what has it been doing (Activity), what can it reach (Sources) — with
// Profile on top as the identity surface and Settings at the foot. Recall is a tab of
// Memories (MemoryTabs), Spaces (browse/join) lives in the namespace picker, Graph is a
// drill-down from an entity, Flow is in the admin drawer.
//
// RAIL_GROUPS, ADMIN_BASE and ADMIN_OPERATOR_ONLY are exported for MobileNav: the phone's
// "More" sheet is generated from these same arrays, so a section added here appears there
// under the same label. MAIN is the flat rail the tests read.
interface NavGroup {
  label: string | null;
  items: NavItem[];
}

export const RAIL_GROUPS: NavGroup[] = [
  // Profile sits first — it's the user's identity surface (the
  // always-loaded context every AI sees).
  { label: null, items: [{ id: "profile", label: "Profile", icon: "cards", scope: "namespace" }] },
  {
    label: "Memory",
    items: [
      { id: "memories", label: "Memories", icon: "clock", scope: "namespace" },
      {
        id: "knowledge",
        label: "Library",
        icon: "document",
        scope: "namespace",
        hint: "in the namespace picked above, or a collection you read",
      },
      // Route id stays "sessions" (stable deep links); the label matches the
      // view it renders — Conversations (threads), not the run-session that
      // groups runs in the Runs view.
      { id: "sessions", label: "Conversations", icon: "chat", scope: "namespace" },
      // Assistant: inspect what a user, agent or session remembers and draft changes to
      // what creates those memories. Read-only.
      { id: "assistant", label: "Assistant", icon: "sparkle", scope: "namespace" },
    ],
  },
  {
    label: "Activity",
    items: [
      // Runs: the ledger of agent executions — goal, what was recalled and
      // relied on, what was written, what it cost, how it ended.
      { id: "runs", label: "Runs", icon: "list-numbered", scope: "namespace" },
      // Tasks: the inbox plane — work one agent handed another, triaged, claimed under a
      // lease, and edited by a person on a board.
      { id: "tasks", label: "Tasks", icon: "email", scope: "org" },
    ],
  },
  {
    label: "Sources",
    // Code: the indexed-repo plane. Not memory — read-only reference data
    // with its own health pipeline, hence a section rather than a namespace.
    items: [{ id: "code", label: "Repositories", icon: "code", scope: "org" }],
  },
];
export const MAIN: NavItem[] = RAIL_GROUPS.flatMap((g) => g.items);
export const SETTINGS_ITEM: NavItem = { id: "setup", label: "Settings", icon: "gear", scope: "namespace" };

// Admin drawer items. Review queue is HIDDEN by default — it's a
// usually-empty surface (conflicts/learnings only appear on
// multi-writer namespaces) and a permanent rail entry advertising
// emptiness is noise. The route stays resolvable for deep links; when there's actually
// something to review the entry slides in automatically via the badge logic below.
//
// Badges are layered on at render time via `visibleAdmin` so the
// labels can come straight from the const while count / warning
// state stays reactive.
// The drawer reads as three workflows — Review (decide something), Observe (see what
// happened), Access (who may) — rather than a flat list. ADMIN_GROUPS is the rendering
// order; ADMIN_BASE stays the flat list MobileNav and the tests read. Guardrails (the
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
      { id: "admin:activity", label: "Activity", icon: "history", scope: "namespace" },
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

export function Sidebar() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const identity = useStore((s) => s.identity);
  const collapsed = useStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useStore((s) => s.toggleSidebar);
  const [adminOpen, setAdminOpen] = useState(isDrawerView(view));
  const [conflictCount, setConflictCount] = useState<number | null>(null);
  // A recent automatic retirement: the queue's Auto-resolved tab has something to show.
  const [autoResolved, setAutoResolved] = useState(false);
  const [proposalWaits, setProposalWaits] = useState(false);
  // Lean notification surface: badge admin items that have actionable
  // state right now. Over-cap on Usage = burning $; new-since-24h on
  // Agents = unexpected client connected.
  const [overCap, setOverCap] = useState(false);
  const [newAgent24hCount, setNewAgent24hCount] = useState(0);
  const newAgentsRef = useRef(0);
  newAgentsRef.current = newAgent24hCount;
  const agentsTick = useRef(0);

  // Operator-only items (Users, which reads admin.users) get appended for an ORG_ADMIN /
  // PLATFORM_ADMIN claim. Auth-disabled dev mode shows them too (the operator IS the user).
  const whoami = useStore((s) => s.whoami);
  const adminBase = [...ADMIN_BASE, ...operatorItems(whoami)];
  // The drawer is for admins of the current namespace (an admin grant from list_mine, or
  // an admin claim), so a viewer never sees an entry with a 403 behind it.
  const myNamespaces = useStore((s) => s.myNamespaces);
  const canAdmin = isNamespaceAdmin(whoami, myNamespaces, identity?.namespace ?? "");
  // The drawer may stay fail-open (a transient list_mine failure must not hide an admin's
  // controls, and deep links still resolve), but the polls that badge it need a POSITIVE
  // admin signal — an admin claim or an active admin grant on this namespace.
  // `isNamespaceAdmin` is true when list_mine failed ("unknown"), and a token-only
  // principal on a jwt-only namespace has no user row, so its list_mine is empty or
  // fails; polling on "unknown" would fire conflicts.list + cost.budget_status every 30 s
  // into AUTH_INSUFFICIENT. Unknown does not poll.
  const roleHere = roleOnNamespace(whoami, myNamespaces, identity?.namespace ?? "");
  // The badges (conflicts and recent auto-resolutions, budget, agents, waiting proposals)
  // are the memory plane's; a code index or a tracker namespace has none, so its admin is
  // not polled for them either.
  const pollsBadges = roleHere === "admin" && isMemoryNamespace(identity?.namespace ?? "", myNamespaces);

  // Layer status badges onto the static ADMIN list. Usage gets a
  // warning indicator (badge=999 + warn tone-styled) when this
  // namespace is over its 24h LLM cap. Agents gets a numeric badge
  // showing how many distinct MCP clients connected in the last 24h
  // with ops > 0 — picks up "did I authorize this MCP connection"
  // anxiety without nagging, with a count so the magnitude is
  // legible.
  const adminWithBadges: NavItem[] = adminBase.map((item) => {
    if (item.id === "admin:usage" && overCap) {
      return { ...item, badge: 999, badgeTone: "warn" as const };
    }
    if (item.id === "admin:agents" && newAgent24hCount > 0) {
      return { ...item, badge: newAgent24hCount, badgeTone: "accent" as const };
    }
    return item;
  });
  const openConflicts = conflictCount != null && conflictCount > 0;
  // The count is of open conflicts; recent auto-resolutions alone show the entry, no number.
  const reviewBadge = openConflicts ? conflictCount : undefined;
  const visibleAdmin: NavItem[] = !canAdmin
    ? []
    : openConflicts || autoResolved || proposalWaits
      ? [
          // Open conflicts warn; recent auto-resolutions use the Auto-resolved tab's icon,
          // and proposals alone the Proposals tab's.
          {
            id: "admin:review_queue",
            label: "Review queue",
            icon: openConflicts ? "warning" : autoResolved ? "flash" : "user",
            badge: reviewBadge,
          },
          ...adminWithBadges,
        ]
      : adminWithBadges;

  // Fetch conflict count + recent auto-resolutions + waiting proposals + budget + agents
  // activity on identity change and refresh every 60s. All of them are fail-quiet — a missing
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
    const last = readSession<{
      conflicts: number | null;
      autoResolved?: boolean;
      proposalWaits?: boolean;
      overCap: boolean;
      newAgents: number;
    }>(badgeKey, BADGE_SESSION_TTL_MS);
    setConflictCount(last ? last.conflicts : null);
    setAutoResolved(last ? !!last.autoResolved : false);
    setProposalWaits(last ? !!last.proposalWaits : false);
    setOverCap(last ? last.overCap : false);
    setNewAgent24hCount(last ? last.newAgents : 0);
  }, [badgeKey]);
  useAfterPaint(() => {
    if (!identity || !pollsBadges) return;
    let cancelled = false;
    const fetch_ = async () => {
      const tasks: Promise<void>[] = [];
      const next = {
        conflicts: null as number | null,
        autoResolved: false,
        proposalWaits: false,
        overCap: false,
        newAgents: 0,
      };
      if (canCall("conflicts.list", roleHere, whoami)) {
        tasks.push(
          // The badge asks for the number, not 100 rows with their values (33 MB per tick
          // on a large namespace).
          countConflicts({ org: identity.org, namespace: identity.namespace })
            .then((n) => {
              if (!cancelled) {
                next.conflicts = n;
                setConflictCount(n);
              }
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
              if (!cancelled) {
                next.autoResolved = found;
                setAutoResolved(found);
              }
            })
            .catch(() => {
              /* hide */
            }),
        );
      }
      if (canCall("memory.proposals", roleHere, whoami)) {
        tasks.push(
          // Whether a proposal waits (the Proposals tab): `limit=0` reads the count alone.
          listProposals({ org: identity.org, namespace: identity.namespace }, 0)
            .then((r) => {
              if (!cancelled) {
                next.proposalWaits = (r.total ?? 0) > 0;
                setProposalWaits(next.proposalWaits);
              }
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
              if (!cancelled) {
                next.overCap = !!s.over_cap;
                setOverCap(!!s.over_cap);
              }
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
              if (cancelled) return;
              next.newAgents = count;
              setNewAgent24hCount(count);
            })
            .catch(() => {
              /* hide */
            }),
        );
      } else {
        next.newAgents = newAgentsRef.current;
      }
      agentsTick.current += 1;
      await Promise.allSettled(tasks);
      if (!cancelled) writeSession(badgeKey, next);
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
      className={`hidden md:flex shrink-0 border-r border-border bg-surface flex-col ${collapsed ? "w-[60px]" : "w-[244px]"}`}
    >
      {/* Brand + collapse — brand is an anchor: cmd-click opens Memories in a new tab. The
          toggle is one button in both states, so it keeps the focus when pressed. */}
      <div className={`pt-5 flex items-center justify-between ${collapsed ? "px-3 pb-2 flex-col gap-4" : "px-5 pb-4"}`}>
        <a
          href={viewHref("memories")}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
            e.preventDefault();
            setView("memories");
          }}
          className="flex items-center gap-2.5 rounded-sm hover:opacity-80 transition-opacity no-underline hover:no-underline"
          aria-label="Loom — go to Memories"
        >
          <LoomMark size={32} />
          {!collapsed && <span className="text-xl font-semibold text-text leading-none">Loom</span>}
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
      <nav className="flex-1 px-3 py-1 overflow-y-auto" aria-label="Sections">
        {RAIL_GROUPS.map((g, gi) => (
          <div
            key={g.label ?? "top"}
            className={`${NAV_LIST} ${gi === 0 ? "" : collapsed ? "pt-2 mt-2 border-t border-border" : "pt-4"}`}
          >
            {g.label && !collapsed && <GroupLabel>{g.label}</GroupLabel>}
            {g.items.map((item) => (
              <NavBtn
                key={item.id}
                item={item}
                active={view === item.id}
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
            >
              <span className={`flex items-center ${NAV_GAP}`}>
                <span className={NAV_ICON}>
                  <Icon name="secure" size="sm" aria-hidden="true" />
                </span>
                Admin
              </span>
              <Icon
                name="chevron-down"
                size="sm"
                aria-hidden="true"
                className={`transition-transform ${adminOpen ? "rotate-180" : ""}`}
              />
            </button>

            {adminOpen && (
              <div className="ml-1 pl-3 border-l border-border mt-0.5 animate-fade-in">
                {groupAdminItems(visibleAdmin).map((g, gi) => (
                  <div key={g.label ?? gi} className={`${NAV_LIST} ${gi > 0 ? "pt-2" : ""}`}>
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

      {/* Settings at the foot: tokens, clients, what is remembered, privacy filters */}
      <div className={`border-t border-border ${collapsed ? "p-2" : "p-3"}`}>
        <NavBtn
          item={SETTINGS_ITEM}
          active={view === "setup"}
          collapsed={collapsed}
          onActivate={() => setView("setup")}
        />
      </div>
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
  "relative flex items-center min-w-0 rounded-(--click-sidebar-navigation-item-radii-all)",
  "py-(--click-sidebar-navigation-item-default-space-y) pl-4 pr-(--click-sidebar-navigation-item-default-space-right)",
  NAV_GAP,
  "[font:var(--click-sidebar-navigation-item-typography-default)]",
  "text-(--click-sidebar-main-navigation-item-color-text-default) hover:text-(--click-sidebar-main-navigation-item-color-text-hover)",
  "bg-(--click-sidebar-main-navigation-item-color-background-default) hover:bg-(--click-sidebar-main-navigation-item-color-background-hover)",
  "aria-[current=page]:bg-(--click-sidebar-main-navigation-item-color-background-active)",
  "no-underline hover:no-underline",
].join(" ");
const NAV_ICON = "inline-flex shrink-0 text-(--click-sidebar-main-navigation-item-color-icon-default)";
const NAV_TITLE =
  "pl-4 pb-1 select-none [font:var(--click-sidebar-navigation-title-typography-default)] text-(--click-sidebar-main-navigation-title-color-default)";

function GroupLabel({ children }: { children: string | null }) {
  if (!children) return null;
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
function NavBadge({
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
