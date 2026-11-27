import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Modal } from "../components/ui/Modal";
import { Unavailable } from "../components/ui/Unavailable";
import { canWrite, needsRole, useNamespaceRole } from "../lib/namespaceRole";
import { openNewDialog } from "../lib/newDialogs";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore, viewHref, type AdminBadges } from "../lib/state";
import { adminItems, groupAdminItems, NavBadge, RAIL_GROUPS, SETTINGS_ITEM, DOCS_ITEM, type NavItem } from "./Sidebar";
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
  badges: AdminBadges | null,
): Array<{ group: string; items: NavItem[] }> {
  const groups = RAIL_GROUPS.map(({ label, items }) => ({
    group: label,
    items: items.filter(({ id }) => !PRIMARY_IDS.includes(id)),
  }));
  const admin = groupAdminItems(adminItems(whoami, mine, namespace, badges));
  groups.push(...admin.map(({ label, items }) => ({ group: label, items })));
  groups.push({ group: "Workspace", items: [SETTINGS_ITEM, DOCS_ITEM] });
  return groups;
}

/** The phone's "More" sheet: New (the header's New menu, which the phone's top bar leaves
 *  out), then every route the sidebar has, in its groups. */
export default function MoreSheet({ onClose }: { onClose: () => void }) {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const whoami = useStore((s) => s.whoami);
  const myNamespaces = useStore((s) => s.myNamespaces);
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  // The sidebar stays mounted below md and reads the badges; the sheet shows its latest answer.
  const badges = useStore((s) => s.adminBadges);
  const groups = mobileMoreItems(whoami, myNamespaces, namespace, badges);
  const libraryElsewhere = useStore((s) => s.libraryElsewhere);
  const role = useNamespaceRole(namespace || undefined);
  // The same rules as the header's New: a read-only role sees both unavailable with the
  // reason, and Document goes while the Library shows a collection (its own Add document
  // adds there).
  const reason = canWrite(role) ? null : needsRole("writer", namespace);
  const add = (which: "memory" | "document") => {
    onClose();
    openNewDialog(which);
  };
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title="More" className="w-full max-w-[520px]">
      {namespace && (
        <section aria-labelledby="more-new" className="mb-4">
          <div id="more-new" className="section-label mb-1.5 px-1">
            New
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Unavailable reason={reason}>
              <Button
                type="secondary"
                htmlType="button"
                iconLeft="sparkle"
                fillWidth
                aria-label="New memory"
                onClick={() => add("memory")}
              >
                Memory
              </Button>
            </Unavailable>
            {!libraryElsewhere && (
              <Unavailable reason={reason}>
                <Button
                  type="secondary"
                  htmlType="button"
                  iconLeft="document"
                  fillWidth
                  aria-label="New document"
                  onClick={() => add("document")}
                >
                  Document
                </Button>
              </Unavailable>
            )}
          </div>
          {reason && <p className="mt-1.5 px-1 text-xs text-text-muted">{reason}</p>}
        </section>
      )}
      {groups.map((g) => (
        <nav key={g.group} aria-label={g.group} className="mb-4">
          <div className="section-label mb-1.5 px-1">{g.group}</div>
          <div className="grid grid-cols-2 gap-2">
            {g.items.map((m) => {
              const active = view === m.id || (m.id === "setup" && view === "connected-accounts");
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
                  className={`flex min-h-11 items-center gap-2 border border-border p-2.5 rounded-sm transition-colors no-underline hover:no-underline text-text ${
                    active ? `${ACTIVE} font-medium` : "bg-surface-2 hover:bg-surface-3"
                  }`}
                >
                  <Icon name={m.icon} size="md" aria-hidden="true" />
                  <span className="min-w-0 text-xs font-medium">{m.label}</span>
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
