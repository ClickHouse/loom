import {
  Suspense,
  lazy,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Popover } from "@clickhouse/click-ui/Popover";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { listMine, setSavedNamespace, cancelNamespaceRequest, isAuthDisabled, type MyNamespaceEntry } from "../api";
import { invalidateShared, sharedMine } from "../lib/sharedReads";
import { useStore } from "../lib/state";
import { WILDCARD_NAMESPACE } from "../lib/namespace";
import { isSystemKind } from "../lib/format";
import { toast, toastError } from "./Toast";
import { confirmAction } from "./ConfirmDialog";
import { Loading, Tip } from "./ui";
import { PopoverTrigger } from "./ui/Menu";
// The create dialog loads when first opened.
const CreateNamespaceDialog = lazy(() =>
  import("../flows/CreateNamespaceDialog").then((m) => ({ default: m.CreateNamespaceDialog })),
);

/**
 * Namespace picker — searchable, sectioned, click-to-open.
 *
 * A flat hover-dropdown works for ~3 namespaces and is unusable past ~20. Sections by
 * role (you own → joined → other), sort by recency within section, badges for visibility,
 * and a hide- system-namespaces toggle so bench artefacts don't drown out a user's actual
 * namespaces.
 */
export function NamespacePicker() {
  const identity = useStore((s) => s.identity);

  // Auth-disabled mode serves exactly one namespace (LOOM_DEFAULT_NAMESPACE):
  // there is nothing to switch to (list_mine has no grants/JWT to build a
  // list from), and a pick would persist server-side to `loom.user_state` —
  // which would silently retarget MCP/SDK writes while the dashboard snapped back to the
  // default. Render the namespace as static context. The menu is its own component so
  // every hook in it runs unconditionally.
  if (isAuthDisabled()) {
    return (
      <div className="inline-flex items-center gap-1.5 h-8 px-3 text-sm text-text">
        <span className="text-text-muted">ns</span>
        <span className="font-medium">{identity?.namespace ?? "local"}</span>
      </div>
    );
  }
  return <NamespacePickerMenu />;
}

function NamespacePickerMenu() {
  const whoami = useStore((s) => s.whoami);
  const identity = useStore((s) => s.identity);
  const setIdentity = useStore((s) => s.setIdentity);
  const setView = useStore((s) => s.setView);
  const pickerRequested = useStore((s) => s.namespacePickerOpen);
  const openNamespacePicker = useStore((s) => s.openNamespacePicker);
  // The rows the app already fetched at boot (App.tsx) seed the list until the picker's
  // own read lands, so a token whose access is grants only (no claim) has a picker at all
  // — and its code/QA rows carry their kind tag from the first paint.
  const stored = useStore((s) => s.myNamespaces);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [showSystem, setShowSystem] = useState(false);
  const [rows, setRows] = useState<MyNamespaceEntry[] | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [openGroups, setOpenGroups] = useState<Record<KindGroup, boolean>>({ code: false, tracker: false });
  const [showAll, setShowAll] = useState<Record<KindGroup, boolean>>({ code: false, tracker: false });
  // The option that holds the list's one tab stop (the last one focused).
  const [active, setActive] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Lazy-load on first open. Refetch when identity.org changes (rare
  // but happens when a multi-tenant user switches orgs upstream).
  useEffect(() => {
    if (!open || !identity) return;
    // The shared read — the header, the banner and the search palette ask the same
    // question on the same page.
    sharedMine(identity.org)
      .then(setRows)
      .catch(() => setRows([]));
  }, [open, identity?.org]);

  const current = identity?.namespace || "—";
  const known = rows ?? stored;
  const currentGroup = kindGroup(known?.find((r) => r.namespace === current));

  // Each open starts folded, except the group the current namespace is in.
  useEffect(() => {
    if (open) {
      setOpenGroups({ code: currentGroup === "code", tracker: currentGroup === "tracker" });
      setShowAll({ code: false, tracker: false });
      setActive(null);
    } else setQuery("");
  }, [open]);

  // The no-namespace landing asks for the picker through the store.
  useEffect(() => {
    if (!pickerRequested) return;
    setOpen(true);
    openNamespacePicker(false);
  }, [pickerRequested, openNamespacePicker]);

  // `*` is an access wildcard, never a namespace to switch into.
  const allowed = (whoami?.namespaces || []).filter((ns) => ns !== WILDCARD_NAMESPACE);

  // Fallback: when list_mine returns nothing (single-user with no
  // memberships, just JWT access), synthesize entries from the
  // whoami.namespaces list so the picker has SOMETHING to show.
  const synthFromJwt: MyNamespaceEntry[] = allowed.map((ns) => ({
    namespace: ns,
    role: "jwt" as const,
    visibility: "private" as const,
    description: "",
    joined_at: null,
    is_new: false,
  }));

  const allRows = known && known.length > 0 ? known : known === null ? null : synthFromJwt;

  // A system / bench namespace: the server's `kind` when it sends one
  // (qa, bench — kept on the namespaces row); the name-shape heuristic only for rows
  // without a kind. Bench-shaped names look like "dx-a1b2c3" (2–4 letters + hex) or
  // "test-*", "snap-*", etc. A tracker namespace the user holds a claim for is not
  // hidden: the Explorer's Patterns mode lives on it and on the code namespaces bound to
  // it; only bench artefacts fold under "system".
  const isSystemShaped = (r: MyNamespaceEntry): boolean => {
    if (r.kind) return isSystemKind(r.kind) && r.kind !== "qa";
    return (
      /^[a-z]{2,4}-[a-f0-9]{6,}$/.test(r.namespace) ||
      /^(test|snap|src|trace|mb|sh|kbd|onboard|bx|ax|cx|dx)-/.test(r.namespace)
    );
  };

  // Every row the list may show, before the search: `*` is an access wildcard, not a
  // namespace, and system / bench rows wait behind their toggle.
  const listed = (allRows || []).filter(
    (r) => r.namespace !== WILDCARD_NAMESPACE && (showSystem || !isSystemShaped(r) || r.namespace === current),
  );
  const q = query.toLowerCase();
  const matches = (r: MyNamespaceEntry) =>
    !q || r.namespace.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q);

  // Memory namespaces by role; code indexes and tracker records each in a folded group of
  // their own, told apart by the server's `kind` alone (a name is free text).
  const memory = listed.filter((r) => kindGroup(r) === null);
  const byRole = (roles: MyNamespaceEntry["role"][]) => memory.filter((r) => roles.includes(r.role));
  const owned = byRole(["admin"]);
  const joined = byRole(["writer", "viewer"]);
  const transient = byRole(["jwt"]);
  const inGroup = (g: KindGroup) => listed.filter((r) => kindGroup(r) === g);
  const kinds = { code: inGroup("code"), tracker: inGroup("tracker") };

  // Sort each group: current selection first, then most-recently-joined.
  const sort = (arr: MyNamespaceEntry[]) =>
    arr.filter(matches).sort((a, b) => {
      if (a.namespace === current) return -1;
      if (b.namespace === current) return 1;
      const at = a.joined_at ? Date.parse(a.joined_at) : 0;
      const bt = b.joined_at ? Date.parse(b.joined_at) : 0;
      return bt - at;
    });
  // Code indexes and tracker records: current first, then by name.
  const sortByName = (arr: MyNamespaceEntry[]) =>
    arr
      .filter(matches)
      .sort((a, b) =>
        a.namespace === current ? -1 : b.namespace === current ? 1 : a.namespace.localeCompare(b.namespace),
      );

  const sections = [
    { title: "Your namespaces", all: owned, rows: sort(owned) },
    { title: "Joined", all: joined, rows: sort(joined) },
    { title: "From your token", all: transient, rows: sort(transient) },
  ];
  // A group is open while a search has matches in it, or once someone opened it.
  const groups = KIND_GROUPS.map((g) => {
    const rows = sortByName(kinds[g]);
    const expanded = q ? rows.length > 0 : openGroups[g];
    const shown = expanded ? (showAll[g] ? rows : rows.slice(0, GROUP_CAP)) : [];
    return { g, all: kinds[g], rows, expanded, shown, more: expanded && rows.length > shown.length };
  }).filter((x) => (q ? x.rows.length > 0 : x.all.length > 0));
  const nothing = sections.every((s) => s.rows.length === 0) && groups.length === 0;

  // One roving tab stop over the options and the group buttons, in the order drawn.
  const navKeys = [
    ...sections.flatMap((s) => s.rows.map((r) => `ns:${r.namespace}`)),
    ...groups.flatMap((x) => [
      `group:${x.g}`,
      ...x.shown.map((r) => `ns:${r.namespace}`),
      ...(x.more ? [`more:${x.g}`] : []),
    ]),
  ];
  const tabStop =
    active && navKeys.includes(active) ? active : navKeys.includes(`ns:${current}`) ? `ns:${current}` : navKeys[0];
  const tabIndexOf = (key: string) => (key === tabStop ? 0 : -1);
  const [fade, updateFade] = useScrollFade(listRef, [open, known, query, openGroups, showAll, showSystem]);

  const systemHiddenCount = !showSystem
    ? (allRows || []).filter((r) => isSystemShaped(r) && r.namespace !== current).length
    : 0;

  if (!whoami || (allowed.length === 0 && !known)) return null;

  const onPick = (ns: string) => {
    if (!identity) return;
    setIdentity({ ...identity, namespace: ns });
    setSavedNamespace(ns);
    setOpen(false);
  };

  const onCancelPending = async (ns: string, role: string) => {
    if (!identity) return;
    if (
      !(await confirmAction({
        title: `Withdraw your ${role} request on ${ns}?`,
        message: "Keeps any current access; the request just leaves the admin queue.",
        confirmLabel: "Withdraw",
      }))
    )
      return;
    try {
      await cancelNamespaceRequest(identity.org, ns);
      toast(`Withdrew ${role} request`, "ok");
      invalidateShared("mine:");
      listMine(identity.org)
        .then(setRows)
        .catch(() => undefined);
    } catch (err) {
      toastError("Cancel failed", err);
    }
  };

  // Up / Down / Home / End rove over the list; Up from the first option returns to the search.
  const navItems = () => [...(listRef.current?.querySelectorAll<HTMLElement>("[data-nav]") ?? [])];
  const onListKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const items = navItems();
    const i = items.indexOf(e.target as HTMLElement);
    if (i < 0) return;
    const to =
      e.key === "ArrowDown"
        ? Math.min(i + 1, items.length - 1)
        : e.key === "ArrowUp"
          ? i - 1
          : e.key === "Home"
            ? 0
            : e.key === "End"
              ? items.length - 1
              : null;
    if (to === null) return;
    e.preventDefault();
    if (to < 0) inputRef.current?.focus();
    else items[to].focus();
  };
  const rowProps = { current, onPick, onCancelPending, tabIndexOf };
  const isCode = currentGroup === "code";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger>
        <button
          ref={triggerRef}
          type="button"
          className="inline-flex items-center gap-1.5 max-w-full min-w-0 h-8 px-2 sm:px-3 rounded-sm hover:bg-surface-2 transition-colors text-sm text-text"
          aria-label={`Namespace: ${current}${isCode ? ", code index" : ""}`}
        >
          {isCode ? (
            <Icon name="code" size="sm" aria-hidden="true" />
          ) : (
            <span className="text-text-muted" aria-hidden="true">
              ns
            </span>
          )}
          <span className="font-medium truncate">{current}</span>
          {isCode && <Badge text="code" state="neutral" size="sm" aria-hidden="true" />}
          <Icon name="chevron-down" size="xs" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <Popover.Content
        align="start"
        aria-label="Namespaces"
        // The search field takes the focus on open, at once: a timer would pull it back from
        // a row someone already arrowed to.
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          inputRef.current?.focus();
        }}
        onCloseAutoFocus={(e) => {
          e.preventDefault();
          triggerRef.current?.focus();
        }}
        className="w-[360px] max-w-[calc(100vw-16px)] max-h-[480px] flex flex-col gap-2"
      >
        <SearchField
          aria-label="Search namespaces"
          ref={inputRef}
          value={query}
          onChange={(v) => setQuery(v)}
          placeholder="Search namespaces…"
          // Down moves into the list, to its tab stop.
          onKeyDown={(e) => {
            if (e.key !== "ArrowDown") return;
            const items = navItems();
            const stop = items.find((el) => el.tabIndex === 0) ?? items[0];
            if (!stop) return;
            e.preventDefault();
            stop.focus();
          }}
        />

        {/* biome-ignore lint/a11y/noStaticElementInteractions: the roving focus of the options inside (APG listbox-like, without role=listbox: the group buttons cannot live in one) */}
        {/* biome-ignore lint/a11y/noNoninteractiveElementInteractions: the same roving focus; the keys go to the buttons inside */}
        <div
          ref={listRef}
          className="scroll-fade flex-1 overflow-y-auto -mx-2"
          data-fade={fade}
          onScroll={updateFade}
          onKeyDown={onListKey}
          onFocus={(e) => {
            const key = (e.target as HTMLElement).dataset.nav;
            if (key) setActive(key);
          }}
        >
          {known === null && <Loading label="Loading namespaces" />}
          {known != null && nothing && (
            <div className="text-xs text-text-muted p-3 text-center">
              {query ? `No namespaces match ‘${query}’` : "No namespaces available."}
            </div>
          )}
          {sections.map((sec) => (
            <Section key={sec.title} title={sec.title} total={sec.all.length} searching={!!q} rows={sec.rows}>
              {sec.rows.map((r) => (
                <NamespaceRow key={r.namespace} r={r} {...rowProps} />
              ))}
            </Section>
          ))}
          {known != null && joined.length === 0 && transient.length === 0 && !query && (
            <Button
              type="empty"
              htmlType="button"
              iconLeft="globe"
              align="left"
              fillWidth
              onClick={() => {
                setOpen(false);
                useStore.getState().setView("browse");
              }}
            >
              Find public namespaces to join in Spaces
            </Button>
          )}
          {groups.map((x) => (
            <GroupDisclosure
              key={x.g}
              group={x.g}
              total={x.all.length}
              matched={x.rows.length}
              searching={!!q}
              expanded={x.expanded}
              tabIndex={tabIndexOf(`group:${x.g}`)}
              onToggle={() => setOpenGroups((o) => ({ ...o, [x.g]: !x.expanded }))}
            >
              {x.shown.map((r) => (
                <NamespaceRow key={r.namespace} r={r} {...rowProps} kindRow />
              ))}
              {x.more && (
                <Button
                  type="empty"
                  htmlType="button"
                  align="left"
                  fillWidth
                  data-nav={`more:${x.g}`}
                  tabIndex={tabIndexOf(`more:${x.g}`)}
                  onClick={() => setShowAll((m) => ({ ...m, [x.g]: true }))}
                >
                  {`Show all ${x.rows.length}`}
                </Button>
              )}
            </GroupDisclosure>
          ))}
        </div>

        <div className="flex flex-col items-stretch border-t border-border pt-2 -mx-2">
          {systemHiddenCount > 0 && (
            <Button type="empty" htmlType="button" align="left" onClick={() => setShowSystem(true)}>
              Show {systemHiddenCount} system / bench namespace{systemHiddenCount === 1 ? "" : "s"}
            </Button>
          )}
          {showSystem && (
            <Button type="empty" htmlType="button" align="left" onClick={() => setShowSystem(false)}>
              Hide system namespaces
            </Button>
          )}
          {/* Browsing and joining spaces is part of picking one,
              so the Spaces page is reached from here, not from the rail. One
              call to action: the empty list above already carries it. */}
          {known != null && (joined.length > 0 || transient.length > 0 || !!query) && (
            <Button
              type="empty"
              htmlType="button"
              iconLeft="globe"
              align="left"
              onClick={() => {
                setOpen(false);
                setView("browse");
              }}
            >
              Browse and join spaces
            </Button>
          )}
          <Button
            type="empty"
            htmlType="button"
            iconLeft="plus"
            align="left"
            onClick={() => {
              setOpen(false);
              setCreateOpen(true);
            }}
          >
            Create namespace
          </Button>
        </div>
      </Popover.Content>
      {createOpen && (
        <Suspense fallback={null}>
          <CreateNamespaceDialog
            open={createOpen}
            onOpenChange={setCreateOpen}
            onCreated={(ns) => {
              // Auto-switch to the just-created namespace + refresh
              // list_mine so the picker reflects the new admin grant
              // immediately, not on next open.
              if (identity) {
                setIdentity({ ...identity, namespace: ns });
                setSavedNamespace(ns);
                invalidateShared("mine:");
                listMine(identity.org)
                  .then(setRows)
                  .catch(() => setRows([]));
              } else {
                setRows(null);
              }
            }}
          />
        </Suspense>
      )}
    </Popover>
  );
}

type KindGroup = "code" | "tracker";
const KIND_GROUPS: KindGroup[] = ["code", "tracker"];
const GROUP_META: Record<KindGroup, { title: string; icon: IconName }> = {
  code: { title: "Code indexes", icon: "code" },
  tracker: { title: "Tracker records", icon: "flag" },
};
// An open group shows this many rows, then "Show all N".
const GROUP_CAP = 8;

/** The folded group a row belongs in, from the server's `kind` only — never its name. */
function kindGroup(r: MyNamespaceEntry | undefined): KindGroup | null {
  if (r?.kind === "code") return "code";
  if (r?.kind === "qa" || r?.kind === "tracker") return "tracker";
  return null;
}

/** "3" — or, while searching, "1 of 3": the group's total, never only the matches. */
function countLabel(matched: number, total: number, searching: boolean): string {
  return searching ? `${matched} of ${total}` : String(total);
}

/** True / false per edge while the list scrolls past it, for the fade mask (styles.css `.scroll-fade`). */
function useScrollFade(ref: RefObject<HTMLElement | null>, deps: unknown[]) {
  const [fade, setFade] = useState<"top" | "bottom" | "both" | undefined>();
  const update = () => {
    const el = ref.current;
    if (!el) return;
    const top = el.scrollTop > 1;
    const bottom = el.scrollTop + el.clientHeight < el.scrollHeight - 1;
    setFade(top && bottom ? "both" : top ? "top" : bottom ? "bottom" : undefined);
  };
  useLayoutEffect(update, deps);
  return [fade, update] as const;
}

function Section({
  title,
  total,
  searching,
  rows,
  children,
}: {
  title: string;
  total: number;
  searching: boolean;
  rows: MyNamespaceEntry[];
  children: ReactNode;
}) {
  const id = useId();
  if (rows.length === 0) return null;
  return (
    <section aria-labelledby={id} className="mb-2 last:mb-0">
      <div id={id} className="section-label px-2 pt-1 pb-0.5">
        {title} <span className="tabular">({countLabel(rows.length, total, searching)})</span>
      </div>
      {children}
    </section>
  );
}

/** A folded group: a native disclosure button, then its rows when open. */
function GroupDisclosure({
  group,
  total,
  matched,
  searching,
  expanded,
  tabIndex,
  onToggle,
  children,
}: {
  group: KindGroup;
  total: number;
  matched: number;
  searching: boolean;
  expanded: boolean;
  tabIndex: number;
  onToggle: () => void;
  children: ReactNode;
}) {
  const id = useId();
  const meta = GROUP_META[group];
  return (
    <section aria-labelledby={`${id}-label`} className="mb-2 last:mb-0">
      <button
        type="button"
        data-nav={`group:${group}`}
        tabIndex={tabIndex}
        aria-expanded={expanded}
        aria-controls={`${id}-rows`}
        onClick={onToggle}
        className="ns-row w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-left text-sm text-text"
      >
        <Icon name={meta.icon} size="sm" aria-hidden="true" />
        <span id={`${id}-label`} className="font-medium">
          {meta.title}
        </span>
        <span className="text-xs text-text-muted tabular">{countLabel(matched, total, searching)}</span>
        <Icon name="chevron-right" size="xs" aria-hidden="true" className={`ml-auto ${expanded ? "rotate-90" : ""}`} />
      </button>
      <div id={`${id}-rows`} hidden={!expanded}>
        {expanded && children}
      </div>
    </section>
  );
}

function NamespaceRow({
  r,
  current,
  onPick,
  onCancelPending,
  tabIndexOf,
  kindRow = false,
}: {
  r: MyNamespaceEntry;
  current: string;
  onPick: (ns: string) => void;
  onCancelPending: (ns: string, role: string) => void;
  tabIndexOf: (key: string) => number;
  /** A code index or tracker record: the description leads, the name is the mono second line. */
  kindRow?: boolean;
}) {
  const active = r.namespace === current;
  const pendingRole = r.pending_role;
  const primary = kindRow && r.description ? r.description : r.namespace;
  const secondary = kindRow ? (r.description ? r.namespace : "") : r.description;
  return (
    <div className="ns-row flex items-center gap-1 rounded-sm" data-current={active || undefined}>
      <button
        type="button"
        data-nav={`ns:${r.namespace}`}
        tabIndex={tabIndexOf(`ns:${r.namespace}`)}
        onClick={() => onPick(r.namespace)}
        aria-current={active ? "true" : undefined}
        className="flex-1 min-w-0 text-left flex items-center gap-2 px-2 py-1.5 rounded-sm text-text"
      >
        <RoleIcon role={r.role} />
        <span className="flex-1 min-w-0">
          <span className={`text-sm truncate block ${kindRow && !r.description ? "font-mono" : ""}`}>{primary}</span>
          {secondary && (
            <span className={`text-xs text-text-muted truncate block ${kindRow ? "font-mono" : ""}`}>{secondary}</span>
          )}
        </span>
        <VisibilityBadge v={r.visibility} />
      </button>
      {pendingRole && (
        <Tip
          content={
            r.pending_admins && r.pending_admins.length > 0
              ? `${r.pending_role} request awaiting approval from: ${r.pending_admins.join(", ")} — click to withdraw`
              : `${r.pending_role} request awaiting admin approval — click to withdraw`
          }
        >
          <Button
            type="secondary"
            htmlType="button"
            onClick={() => onCancelPending(r.namespace, pendingRole)}
            className="shrink-0 mr-1"
          >
            {`pending ${r.pending_role}`}
          </Button>
        </Tip>
      )}
    </div>
  );
}

function RoleIcon({ role }: { role: MyNamespaceEntry["role"] }) {
  if (role === "admin") {
    return <Icon name="star" size="xs" color="var(--global-color-text-muted)" aria-label="You're admin" />;
  }
  return <Icon name="user" size="xs" color="var(--color-text-muted)" aria-label={role} />;
}

function VisibilityBadge({ v }: { v: MyNamespaceEntry["visibility"] }) {
  if (v === "public") {
    return <Icon name="globe" size="xs" color="var(--color-text-muted)" aria-label="Public" />;
  }
  return <Icon name="lock" size="xs" color="var(--color-text-muted)" aria-label="Private" />;
}
