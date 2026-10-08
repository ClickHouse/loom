import { useState } from "react";
import { Clock, FileText, MoreHorizontal, X } from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";
import { useStore, viewHref, type View } from "../lib/state";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { hasOrgAdminClaim, isNamespaceAdmin } from "../lib/role";
import { ADMIN_BASE, ADMIN_OPERATOR_ONLY, RAIL_GROUPS, SETTINGS_ITEM, type NavItem } from "./Sidebar";

/**
 * Phone navigation (below `md`): two primary tabs and a "More" sheet.
 *
 * The sheet is generated from the sidebar's own nav arrays rather than a second
 * hand-written list, which drifts. Whatever the sidebar shows, the phone shows, under the
 * same label and the same route.
 */
const PRIMARY_IDS: View[] = ["memories", "knowledge"];

const PRIMARY: NavItem[] = [
  { id: "memories", label: "Memories", icon: Clock },
  { id: "knowledge", label: "Library", icon: FileText },
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
        className="md:hidden h-14 border-t border-[var(--color-border)] bg-[var(--color-surface)] flex items-center justify-around shrink-0"
      >
        {PRIMARY.map((p) => {
          const Icon = p.icon;
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
              className={`flex flex-col items-center justify-center gap-0.5 flex-1 h-full transition-colors no-underline ${
                active ? "text-[var(--color-accent)]" : "text-[var(--color-text-muted)]"
              }`}
              aria-current={active ? "page" : undefined}
            >
              <Icon size={18} aria-hidden="true" />
              <span className="text-2xs font-medium">{p.label}</span>
            </a>
          );
        })}
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-haspopup="dialog"
          aria-expanded={open}
          className={`flex flex-col items-center justify-center gap-0.5 flex-1 h-full ${
            moreActive ? "text-[var(--color-accent)]" : "text-[var(--color-text-muted)]"
          }`}
        >
          <MoreHorizontal size={18} aria-hidden="true" />
          <span className="text-2xs font-medium">More</span>
        </button>
      </nav>

      <Dialog.Root open={open} onOpenChange={setOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-overlay backdrop-blur-sm animate-fade-in motion-reduce:animate-none!" />
          <Dialog.Content
            aria-describedby={undefined}
            className="fixed bottom-0 left-0 right-0 z-50 bg-[var(--color-surface)] rounded-t-2xl border-t border-[var(--color-border)] animate-slide-in motion-reduce:animate-none! p-4 max-h-[80vh] overflow-y-auto"
          >
            <div className="flex items-center justify-between mb-3">
              <Dialog.Title className="text-md font-semibold">More</Dialog.Title>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="p-1 text-[var(--color-text-muted)]"
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>
            {groups.map((g) => (
              <nav key={g.group} aria-label={g.group} className="mb-4">
                <div className="text-2xs uppercase tracking-wider text-[var(--color-text-muted)] mb-1.5 px-1">
                  {g.group}
                </div>
                <div className="grid grid-cols-3 gap-2">
                  {g.items.map((m) => {
                    const Icon = m.icon;
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
                        className={`flex flex-col items-center justify-center gap-1.5 py-3 rounded-lg transition-colors no-underline ${
                          active
                            ? "bg-[var(--color-accent-soft)] text-[var(--color-accent)] font-medium"
                            : "bg-[var(--color-surface-2)] hover:bg-[var(--color-surface-3)] text-[var(--color-text)]"
                        }`}
                      >
                        <Icon size={20} aria-hidden="true" />
                        <span className="text-2xs font-medium text-center">{m.label}</span>
                      </a>
                    );
                  })}
                </div>
              </nav>
            ))}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}
