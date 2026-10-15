import { useEffect, useRef, useState } from "react";
import {
  Clock,
  AlertTriangle,
  FileText,
  Settings,
  Activity,
  History,
  DollarSign,
  Shield,
  ChevronDown,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  Users as UsersIcon,
  ArrowLeftRight,
  Layers,
  MessagesSquare,
  Ban,
  Code,
  ListChecks,
  Gavel,
  Inbox,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Tip } from "../components/ui/Tooltip";
import { useStore, viewHref, type View } from "../lib/state";
import { countConflicts, countNewAgents } from "../api";
import { readSession, useAfterPaint, writeSession } from "../lib/afterPaint";
import { sharedBudgetStatus } from "../lib/sharedReads";

// How long a remembered badge answer is shown before the idle refresh replaces it — one
// poll interval.
const BADGE_SESSION_TTL_MS = 60_000;
// The agents badge reads every fifth minute tick
const AGENTS_EVERY_N_TICKS = 5;
import { hasOrgAdminClaim, isNamespaceAdmin } from "../lib/role";
import { roleOnNamespace } from "../lib/namespaceRole";
import { canCall } from "../lib/canCall";
import { isMemoryNamespace } from "../lib/namespace";
// Loom mark inlined so currentColor inherits from the sidebar's text
// color and the icon stays visible on both light and dark themes.
function LoomMark({ size = 32 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 40 40"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="shrink-0 text-[var(--color-accent)]"
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
  icon: LucideIcon;
  /** What the page is scoped by — the namespace picked in the header, or the whole org
   *  (Repositories, Tasks). Read out in the item's tooltip so a namespace switch never
   *  surprises. */
  scope?: "namespace" | "org";
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
  { label: null, items: [{ id: "profile", label: "Profile", icon: Layers, scope: "namespace" }] },
  {
    label: "Memory",
    items: [
      { id: "memories", label: "Memories", icon: Clock, scope: "namespace" },
      { id: "knowledge", label: "Library", icon: FileText, scope: "namespace" },
      // Route id stays "sessions" (stable deep links); the label matches the
      // view it renders — Conversations (threads), not the run-session that
      // groups runs in the Runs view.
      { id: "sessions", label: "Conversations", icon: MessagesSquare, scope: "namespace" },
    ],
  },
  {
    label: "Activity",
    items: [
      // Runs: the ledger of agent executions — goal, what was recalled and
      // relied on, what was written, what it cost, how it ended.
      { id: "runs", label: "Runs", icon: ListChecks, scope: "namespace" },
      // Tasks: the inbox plane — work one agent handed another, triaged, claimed under a
      // lease, and edited by a person on a board.
      { id: "tasks", label: "Tasks", icon: Inbox, scope: "org" },
    ],
  },
  {
    label: "Sources",
    // Code: the indexed-repo plane. Not memory — read-only reference data
    // with its own health pipeline, hence a section rather than a namespace.
    items: [{ id: "code", label: "Repositories", icon: Code, scope: "org" }],
  },
];
export const MAIN: NavItem[] = RAIL_GROUPS.flatMap((g) => g.items);
export const SETTINGS_ITEM: NavItem = { id: "setup", label: "Settings", icon: Settings, scope: "namespace" };

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
      { id: "admin:guardrails", label: "Guardrails", icon: Ban, scope: "namespace" },
      // Policies: history rules over what an agent may do next — forbid /
      // require an action based on the caller's own recent trace.
      { id: "admin:policies", label: "Policies", icon: Gavel, scope: "namespace" },
    ],
  },
  {
    label: "Observe",
    items: [
      { id: "admin:health", label: "Health", icon: Activity, scope: "namespace" },
      { id: "admin:activity", label: "Activity", icon: History, scope: "namespace" },
      // Flow (route id "sharing" — stable deep links): a READ-ONLY observability
      // view — where memories flow (writes/recalls, cross-namespace reach,
      // per-tool producer/consumer activity, memberships).
      { id: "sharing", label: "Flow", icon: ArrowLeftRight, scope: "namespace" },
      { id: "admin:usage", label: "Usage", icon: DollarSign, scope: "namespace" },
      { id: "admin:agents", label: "Agents", icon: Plug, scope: "namespace" },
    ],
  },
];
export const ADMIN_BASE: NavItem[] = ADMIN_GROUPS.flatMap((g) => g.items);

/** A view that lives in the admin drawer: the `admin:` ids and the routes
 * filed there under a stable non-prefixed id (Flow). The drawer opens on a deep link to
 *  any of them. */
export function isDrawerView(v: View): boolean {
  return v.startsWith("admin:") || ADMIN_BASE.some((i) => i.id === v) || ADMIN_OPERATOR_ONLY.some((i) => i.id === v);
}

// Operator-only items — shown only when the caller's JWT carries
// PLATFORM_ADMIN / ORG_ADMIN. The Users view is the most-load-bearing
// of these (you can't manage who has access without seeing the list).
export const ADMIN_OPERATOR_ONLY: NavItem[] = [{ id: "admin:users", label: "Users", icon: UsersIcon, scope: "org" }];
/** The drawer's Access group: operator entries, shown to platform / org admins. */
const ACCESS_GROUP: NavGroup = { label: "Access", items: ADMIN_OPERATOR_ONLY };

/** The drawer items a caller may see, in their groups — Review queue leads Review while
 *  there is something to review; the operator-only Access group only for platform / org
 *  admins. */
export function groupAdminItems(visible: NavItem[]): NavGroup[] {
  const byId = new Map(visible.map((i) => [i.id, i] as const));
  const out: NavGroup[] = [];
  const review = byId.get("admin:review_queue");
  for (const g of [...ADMIN_GROUPS, ACCESS_GROUP]) {
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
  const adminBase = hasOrgAdminClaim(whoami) ? [...ADMIN_BASE, ...ADMIN_OPERATOR_ONLY] : ADMIN_BASE;
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
  // The badges count conflicts, budget and agents of the memory plane; a code index or a
  // tracker namespace has none, so its admin is not polled for them either.
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
    : openConflicts || autoResolved
      ? [
          { id: "admin:review_queue", label: "Review queue", icon: AlertTriangle, badge: reviewBadge },
          ...adminWithBadges,
        ]
      : adminWithBadges;

  // Fetch conflict count + recent auto-resolutions + budget + agents activity on identity
  // change and refresh every 60s. All four are fail-quiet — a missing
  // backend op just leaves its badge hidden. Only for a known admin of this
  // namespace: otherwise a viewer-only token draws AUTH_INSUFFICIENT 403s on every page
  // load, and a token-only principal through the fail-open gate. Each call also passes
  // `canCall` so the rule lives in one place. The three reads leave AFTER the page's
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
    const last = readSession<{ conflicts: number | null; autoResolved?: boolean; overCap: boolean; newAgents: number }>(
      badgeKey,
      BADGE_SESSION_TTL_MS,
    );
    setConflictCount(last ? last.conflicts : null);
    setAutoResolved(last ? !!last.autoResolved : false);
    setOverCap(last ? last.overCap : false);
    setNewAgent24hCount(last ? last.newAgents : 0);
  }, [badgeKey]);
  useAfterPaint(() => {
    if (!identity || !pollsBadges) return;
    let cancelled = false;
    const fetch_ = async () => {
      const tasks: Promise<void>[] = [];
      const next = { conflicts: null as number | null, autoResolved: false, overCap: false, newAgents: 0 };
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
      if (canCall("audit.list", roleHere, whoami)) {
        tasks.push(
          // The newest auto-resolve record: enough to know the Auto-resolved tab is not empty.
          // Imported on demand so the admin API stays out of the index chunk.
          import("../api/admin")
            .then(({ hasAutoResolved }) => hasAutoResolved({ org: identity.org, namespace: identity.namespace }))
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
      className={`hidden md:flex shrink-0 border-r border-[var(--color-border)] bg-[var(--color-surface)] flex-col ${collapsed ? "w-[60px]" : "w-[244px]"}`}
    >
      {/* Brand + collapse — brand is an anchor: cmd-click opens Memories in a new tab */}
      <div className={`pt-5 pb-4 flex items-center justify-between ${collapsed ? "px-3" : "px-5"}`}>
        <a
          href={viewHref("memories")}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
            e.preventDefault();
            setView("memories");
          }}
          className="flex items-center gap-2.5 rounded-lg hover:opacity-80 transition-opacity outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)] no-underline"
          aria-label="Loom — go to Memories"
        >
          <LoomMark size={32} />
          {!collapsed && (
            <span className="text-xl font-semibold tracking-tight text-[var(--color-text)] leading-none">Loom</span>
          )}
        </a>
        {!collapsed && (
          <button
            onClick={toggleSidebar}
            className="p-1 text-[var(--color-text-subtle)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded transition-colors"
            aria-label="Collapse sidebar"
          >
            <PanelLeftClose size={14} />
          </button>
        )}
      </div>
      {collapsed && (
        <button
          onClick={toggleSidebar}
          className="mx-3 mb-2 p-1.5 text-[var(--color-text-subtle)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded transition-colors"
          aria-label="Expand sidebar"
        >
          <PanelLeftOpen size={14} />
        </button>
      )}

      {/* Main nav — anchors so cmd-click opens in a new tab */}
      <nav className="flex-1 px-3 mt-1 overflow-y-auto" aria-label="Sections">
        {RAIL_GROUPS.map((g, gi) => (
          <div
            key={g.label ?? "top"}
            className={
              gi === 0
                ? "space-y-1"
                : `space-y-1 ${collapsed ? "pt-2 mt-2 border-t border-[var(--color-border)]" : "pt-4"}`
            }
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
              onClick={() => setAdminOpen((v) => !v)}
              className="w-full flex items-center justify-between px-3 py-2 text-md rounded-lg text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)] transition-colors"
              aria-expanded={adminOpen}
            >
              <span className="flex items-center gap-2.5">
                <Shield size={15} /> Admin
              </span>
              <ChevronDown size={14} className={`transition-transform ${adminOpen ? "rotate-180" : ""}`} />
            </button>

            {adminOpen && (
              <div className="ml-1 pl-3 border-l border-[var(--color-border)] mt-0.5 animate-fade-in motion-reduce:animate-none!">
                {groupAdminItems(visibleAdmin).map((g, gi) => (
                  <div key={g.label ?? gi} className={`space-y-0.5 ${gi > 0 ? "pt-2" : ""}`}>
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
          <div className="pt-3 mt-2 border-t border-[var(--color-border)] space-y-1">
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
      <div className={`border-t border-[var(--color-border)] ${collapsed ? "p-2" : "p-3"}`}>
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

function GroupLabel({ children }: { children: string | null }) {
  if (!children) return null;
  return (
    <div className="px-3 pb-1 text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-subtle)] select-none">
      {children}
    </div>
  );
}

/** The scope read-out for an item's tooltip / title. */
export function scopeHint(item: NavItem): string {
  if (item.scope === "org") return "across every namespace";
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
  const Icon = item.icon;
  const hint = scopeHint(item);
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
      className={`w-full flex items-center ${collapsed ? "justify-center" : "justify-between"} gap-2 ${collapsed ? "px-2 py-2" : "px-3 py-2"} text-md rounded-lg transition-colors relative no-underline ${
        active
          ? "bg-[var(--color-accent-soft)] text-[var(--color-accent)] font-medium"
          : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]"
      }`}
      aria-label={collapsed ? item.label : undefined}
      aria-current={active ? "page" : undefined}
      title={!collapsed && hint ? `${item.label} — ${hint}` : undefined}
    >
      <span className="flex items-center gap-2.5 min-w-0">
        <Icon size={15} className="shrink-0" />
        {!collapsed && <span className="truncate">{item.label}</span>}
      </span>
      {item.badge != null &&
        item.badge > 0 &&
        (() => {
          const tone = item.badgeTone || "default";
          const isDot = item.badge >= 999;
          const colorCls =
            tone === "warn"
              ? "bg-[var(--color-warn)] text-[var(--color-on-accent)]"
              : tone === "accent"
                ? "bg-[var(--color-accent)] text-[var(--color-on-accent)]"
                : "bg-[var(--color-accent)] text-[var(--color-on-accent)]";
          if (isDot) {
            return (
              <span
                aria-label="Attention required"
                title="Attention required"
                className={`shrink-0 w-2 h-2 rounded-full ${colorCls.split(" ")[0]} ${
                  collapsed ? "absolute top-0.5 right-0.5" : ""
                }`}
              />
            );
          }
          return (
            <span
              className={`text-2xs font-semibold tabular px-1.5 py-0.5 rounded ${colorCls} ${
                collapsed ? "absolute -top-0.5 -right-0.5 min-w-[16px] h-[16px] grid place-items-center px-0.5" : ""
              }`}
            >
              {item.badge}
            </span>
          );
        })()}
    </a>
  );
  return (
    <Tip content={item.label} side="right" disabled={!collapsed}>
      {anchor}
    </Tip>
  );
}
