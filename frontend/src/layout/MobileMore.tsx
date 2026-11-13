import { Icon } from "@clickhouse/click-ui/Icon";
import { Modal } from "../components/ui/Modal";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore, viewHref } from "../lib/state";
import {
  adminItems,
  badgeKeyOf,
  NavBadge,
  rememberedBadges,
  RAIL_GROUPS,
  SETTINGS_ITEM,
  type AdminBadges,
  type NavItem,
} from "./Sidebar";
import { ACTIVE, PRIMARY_IDS } from "./mobileTabs";

// The sheet is generated from the sidebar's own nav arrays rather than a second hand-written
// list, which drifts. Whatever the sidebar shows, the phone shows, under the same label and
// the same route.

/**
 * Everything the sidebar routes to, grouped the way the sheet shows it. The Admin group is
 * the sidebar's own admin list (`adminItems`), with the badges the sidebar last read.
 */
export function mobileMoreItems(
  whoami: WhoAmI | null,
  mine: MyNamespaceEntry[] | null,
  namespace: string,
  badges: AdminBadges | null = null,
): Array<{ group: string; items: NavItem[] }> {
  // The sheet shows the rail's own groups; the two primary tabs are left out of theirs,
  // and Settings closes the list. An unlabelled rail group (Profile) joins the first
  // labelled one on the phone rather than heading a one-item group of its own.
  const groups: Array<{ group: string; items: NavItem[] }> = [];
  let carry: NavItem[] = [];
  for (const g of RAIL_GROUPS) {
    const items = [...carry, ...g.items.filter((m) => !PRIMARY_IDS.includes(m.id))];
    if (!g.label) {
      carry = items;
      continue;
    }
    carry = [];
    if (items.length > 0) groups.push({ group: g.label, items });
  }
  if (carry.length > 0) groups.push({ group: "Main", items: carry });
  groups.push({ group: "Settings", items: [SETTINGS_ITEM] });
  const admin = adminItems(whoami, mine, namespace, badges);
  if (admin.length > 0) groups.push({ group: "Admin", items: admin });
  return groups;
}

/** The phone's "More" sheet: every route the sidebar has, in its groups. */
export default function MoreSheet({ onClose }: { onClose: () => void }) {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const whoami = useStore((s) => s.whoami);
  const myNamespaces = useStore((s) => s.myNamespaces);
  const identity = useStore((s) => s.identity);
  // The sidebar stays mounted below md and reads the badges; the sheet shows its last answer.
  const groups = mobileMoreItems(
    whoami,
    myNamespaces,
    identity?.namespace ?? "",
    rememberedBadges(badgeKeyOf(identity)),
  );
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title="More" className="w-full max-w-[520px]">
      {groups.map((g) => (
        <nav key={g.group} aria-label={g.group} className="mb-4">
          <div className="section-label mb-1.5 px-1">{g.group}</div>
          <div className="grid grid-cols-3 gap-2">
            {g.items.map((m) => {
              const active = view === m.id;
              return (
                <a
                  key={m.id}
                  href={viewHref(m.id)}
                  onClick={(e) => {
                    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                    e.preventDefault();
                    setView(m.id);
                    onClose();
                  }}
                  aria-current={active ? "page" : undefined}
                  className={`flex flex-col items-center justify-center gap-1.5 py-3 rounded-sm transition-colors no-underline hover:no-underline text-text ${
                    active ? `${ACTIVE} font-medium` : "bg-surface-2 hover:bg-surface-3"
                  }`}
                >
                  <Icon name={m.icon} size="md" aria-hidden="true" />
                  <span className="text-xs font-medium text-center">{m.label}</span>
                  {!!m.badge && <NavBadge count={m.badge} tone={m.badgeTone ?? "default"} collapsed={false} />}
                </a>
              );
            })}
          </div>
        </nav>
      ))}
    </Modal>
  );
}
