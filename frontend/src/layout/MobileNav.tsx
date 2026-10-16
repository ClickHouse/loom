import { useState } from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Modal } from "../components/ui/Modal";
import { useStore, viewHref, type View } from "../lib/state";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { hasOrgAdminClaim, isNamespaceAdmin } from "../lib/role";
import { ADMIN_BASE, ADMIN_OPERATOR_ONLY, RAIL_GROUPS, SETTINGS_ITEM, type NavItem } from "./Sidebar";

/**
 * Phone navigation (below `md`): two primary tabs and a "More" dialog (Click UI's dialog
 * fills the screen below 640 px).
 *
 * The sheet is generated from the sidebar's own nav arrays rather than a second
 * hand-written list, which drifts. Whatever the sidebar shows, the phone shows, under the
 * same label and the same route.
 */
const PRIMARY_IDS: View[] = ["memories", "knowledge"];

const PRIMARY: NavItem[] = [
  { id: "memories", label: "Memories", icon: "clock" },
  { id: "knowledge", label: "Library", icon: "document" },
];

/**
 * Everything the sidebar routes to, grouped the way the sheet shows it. The
 * Admin group follows the sidebar's gate: admins of the current namespace only, operator
 * entries for platform / org admins.
 */
export function mobileMoreItems(
  whoami: WhoAmI | null,
  mine: MyNamespaceEntry[] | null,
  namespace: string,
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
  if (isNamespaceAdmin(whoami, mine, namespace)) {
    const admin = hasOrgAdminClaim(whoami) ? [...ADMIN_BASE, ...ADMIN_OPERATOR_ONLY] : ADMIN_BASE;
    groups.push({ group: "Admin", items: admin });
  }
  return groups;
}

export function MobileNav() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const whoami = useStore((s) => s.whoami);
  const myNamespaces = useStore((s) => s.myNamespaces);
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  const [open, setOpen] = useState(false);
  const groups = mobileMoreItems(whoami, myNamespaces, namespace);
  const moreActive = !PRIMARY_IDS.includes(view);

  return (
    <>
      <nav
        aria-label="Sections"
        className="md:hidden h-14 border-t border-border bg-surface flex items-center justify-around shrink-0"
      >
        {PRIMARY.map((p) => {
          const active = view === p.id;
          return (
            <a
              key={p.id}
              href={viewHref(p.id)}
              onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                e.preventDefault();
                setView(p.id);
              }}
              className={`relative flex flex-col items-center justify-center gap-0.5 flex-1 h-full transition-colors no-underline hover:no-underline ${
                active ? "bg-surface-3 text-text" : "text-text-muted"
              }`}
              aria-current={active ? "page" : undefined}
            >
              {/* The current tab carries the selected fill and a bar, not only a text colour. */}
              {active && <span aria-hidden="true" className="absolute inset-x-3 top-0 h-0.5 rounded-b bg-text" />}
              <Icon name={p.icon} size="md" aria-hidden="true" />
              <span className="text-xs font-medium">{p.label}</span>
            </a>
          );
        })}
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-haspopup="dialog"
          aria-expanded={open}
          className={`relative flex flex-col items-center justify-center gap-0.5 flex-1 h-full ${
            moreActive ? "bg-surface-3 text-text" : "text-text-muted"
          }`}
        >
          {moreActive && <span aria-hidden="true" className="absolute inset-x-3 top-0 h-0.5 rounded-b bg-text" />}
          <Icon name="dots-horizontal" size="md" aria-hidden="true" />
          <span className="text-xs font-medium">More</span>
        </button>
      </nav>

      <Modal open={open} onOpenChange={setOpen} title="More" className="w-full max-w-[520px]">
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
                      setOpen(false);
                    }}
                    aria-current={active ? "page" : undefined}
                    className={`flex flex-col items-center justify-center gap-1.5 py-3 rounded-sm transition-colors no-underline hover:no-underline text-text ${
                      active ? "bg-surface-3 font-medium" : "bg-surface-2 hover:bg-surface-3"
                    }`}
                  >
                    <Icon name={m.icon} size="md" aria-hidden="true" />
                    <span className="text-xs font-medium text-center">{m.label}</span>
                  </a>
                );
              })}
            </div>
          </nav>
        ))}
      </Modal>
    </>
  );
}
