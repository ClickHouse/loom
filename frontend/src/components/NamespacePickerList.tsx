import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { listMine, cancelNamespaceRequest, type MyNamespaceEntry } from "../api";
import { invalidateShared, sharedMine } from "../lib/sharedReads";
import { useStore } from "../lib/state";
import { WILDCARD_NAMESPACE, isCollectionName } from "../lib/namespace";
import { nameFamilies } from "../lib/nameFamilies";
import { isSystemKind, parseChTime } from "../lib/format";
import { toast, toastError } from "./Toast";
import { confirmAction } from "./ConfirmDialog";
import { Loading, Tip } from "./ui";

/**
 * The namespace picker's open panel: the search, the list and its footer
 * (`NamespacePicker` draws the trigger and loads this the first time it is needed). Sections
 * by role (you own → joined → other), sort by recency within section, badges for
 * visibility, and a hide-system-namespaces toggle so bench artefacts don't drown out a
 * user's actual namespaces. Memory namespaces whose names share a prefix (`u-`, a
 * deployment's own scheme, per owner when the names carry its uuid) fold into a group per
 * prefix, as repositories and tracker records do. It mounts as the popover opens, so each
 * open starts folded, with the search empty.
 */
export function NamespacePickerList({
  inputRef,
  triggerRef,
  onPick,
  onClose,
  onCreate,
}: {
  /** The search field, which the popover focuses as it opens. */
  inputRef: RefObject<HTMLInputElement>;
  /** The picker's trigger, which holds the focus while this panel loads. */
  triggerRef: RefObject<HTMLButtonElement>;
  onPick: (namespace: string) => void;
  /** Close the popover (before a jump to Spaces). */
  onClose: () => void;
  /** Close the popover and open the create dialog. */
  onCreate: () => void;
}) {
  const whoami = useStore((s) => s.whoami);
  const identity = useStore((s) => s.identity);
  const setView = useStore((s) => s.setView);
  // The rows the app already fetched at boot (App.tsx) seed the list until the picker's
  // own read lands, so a token whose access is grants only (no claim) has a picker at all
  // — and its code/QA rows carry their kind tag from the first paint.
  const stored = useStore((s) => s.myNamespaces);
  const [query, setQuery] = useState("");
  const [showSystem, setShowSystem] = useState(false);
  const [rows, setRows] = useState<MyNamespaceEntry[] | null>(null);
  // Keyed by group: "code", "tracker" or a name prefix ("team-").
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  const [showAll, setShowAll] = useState<Record<string, boolean>>({});
  // A group someone folded or opened while searching: it overrides the search's own opening
  // until the search is cleared.
  const [searchOpen, setSearchOpen] = useState<Record<string, boolean>>({});
  // The row "Show all" hands the focus to, once it is drawn (the button itself goes away).
  const [focusNav, setFocusNav] = useState<string | null>(null);
  // The option that holds the list's one tab stop (the last one focused).
  const [active, setActive] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Read on each open, again when identity.org changes (rare, but happens when a
  // multi-tenant user switches orgs upstream). The shared read: the header, the banner and
  // the search palette ask the same question on the same page.
  useEffect(() => {
    if (!identity) return;
    sharedMine(identity.org)
      .then(setRows)
      .catch(() => setRows([]));
  }, [identity?.org]);

  const current = identity?.namespace || "—";
  const known = rows ?? stored;

  // The search field takes the focus: the popover finds none to focus while this panel
  // still loads. Only from where the open left it (the trigger, or nothing): someone who
  // moved on meanwhile keeps their place.
  useEffect(() => {
    const focused = document.activeElement;
    if (!focused || focused === document.body || focused === triggerRef.current) inputRef.current?.focus();
  }, []);

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
  // namespace, a collection opens in the Library (the footer links there), and system /
  // bench rows wait behind their toggle.
  const listed = (allRows || []).filter(
    (r) =>
      r.namespace !== WILDCARD_NAMESPACE &&
      !isCollectionName(r.namespace) &&
      (showSystem || !isSystemShaped(r) || r.namespace === current),
  );
  const q = query.toLowerCase();
  const matches = (r: MyNamespaceEntry) =>
    !q || r.namespace.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q);

  // Repositories and tracker records each fold into a group of their own, told apart by the
  // server's `kind` alone (a name is free text); memory namespaces whose names share a
  // token prefix fold into one per prefix (`nameFamilies`). The names decide the groups, so
  // a keystroke in the search reworks none of them.
  const memoryNames = listed.filter((r) => !kindGroup(r)).map((r) => r.namespace);
  const families = useMemo(() => nameFamilies(memoryNames), [memoryNames.join("\n")]);
  const groupOf = (r: MyNamespaceEntry | undefined): string | null =>
    kindGroup(r) ?? (r && families.get(r.namespace)) ?? null;
  const currentGroup = groupOf(listed.find((r) => r.namespace === current));
  // Each open starts folded, except the group the current namespace is in as it opens; a
  // group someone folds or opens keeps that (`openGroups`). Boot's rows decide that group at
  // once when they put the current namespace in one, and this panel's own read decides it
  // for good as it answers: boot's rows may be stale, or missing.
  const openAtFirst = useRef<{ group: string | null; settled: boolean }>({ group: null, settled: false });
  if (!openAtFirst.current.settled && (currentGroup !== null || rows !== null)) {
    openAtFirst.current = { group: currentGroup, settled: rows !== null };
  }
  // One pass: the memory namespaces outside every group (by role, below), and each group's.
  const memory: MyNamespaceEntry[] = [];
  const byGroup = new Map<string, MyNamespaceEntry[]>();
  for (const r of listed) {
    const g = groupOf(r);
    if (!g) memory.push(r);
    else if (byGroup.has(g)) byGroup.get(g)?.push(r);
    else byGroup.set(g, [r]);
  }
  const byRole = (roles: MyNamespaceEntry["role"][]) => memory.filter((r) => roles.includes(r.role));
  const owned = byRole(["admin"]);
  const joined = byRole(["writer", "viewer"]);
  const transient = byRole(["jwt"]);
  // Joined anything, grouped or not: the Spaces link moves from the list to the footer.
  const hasJoined = listed.some((r) => !kindGroup(r) && r.role !== "admin");

  // Sort each group: current selection first, then most-recently-joined.
  const sort = (arr: MyNamespaceEntry[]) =>
    arr.filter(matches).sort((a, b) => {
      if (a.namespace === current) return -1;
      if (b.namespace === current) return 1;
      const at = parseChTime(a.joined_at)?.getTime() ?? 0;
      const bt = parseChTime(b.joined_at)?.getTime() ?? 0;
      return bt - at;
    });
  // A group's rows: current first, then by name.
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
  // A group is open while a search has matches in it (unless someone folded it), or once
  // someone opened it. Name families come first, by prefix: they are memory namespaces.
  const groups = [...[...new Set(families.values())].sort(), ...KIND_GROUPS]
    .map((g) => {
      const all = byGroup.get(g) ?? [];
      const rows = sortByName(all);
      const expanded = q ? (searchOpen[g] ?? rows.length > 0) : (openGroups[g] ?? g === openAtFirst.current.group);
      const shown = expanded ? (showAll[g] ? rows : rows.slice(0, GROUP_CAP)) : [];
      return { g, all, rows, expanded, shown, more: expanded && rows.length > shown.length };
    })
    .filter((x) => (q ? x.rows.length > 0 : x.all.length > 0));
  const nothing = sections.every((s) => s.rows.length === 0) && groups.length === 0;

  // One roving tab stop over the options and the group buttons, in the order drawn. A
  // pending request's withdraw button is a tab stop of its own: an action, not an option.
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
  const [fade, updateFade] = useScrollFade(listRef, [known, query, openGroups, searchOpen, showAll, showSystem]);

  useLayoutEffect(() => {
    if (!focusNav) return;
    listRef.current?.querySelector<HTMLElement>(`[data-nav="${CSS.escape(focusNav)}"]`)?.focus();
    setFocusNav(null);
  }, [focusNav]);

  const systemHiddenCount = !showSystem
    ? (allRows || []).filter((r) => isSystemShaped(r) && r.namespace !== current).length
    : 0;

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

  return (
    <>
      <SearchField
        aria-label="Search namespaces"
        ref={inputRef}
        value={query}
        onChange={(v) => {
          setQuery(v);
          if (!v) setSearchOpen({});
        }}
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
        // Stretched across the popover: its panel is a column flex that aligns to the start,
        // and the rows' hover and current fills span the list. It clips its overflow: 4 px
        // above and below keep the rings of the first and last controls whole.
        className="scroll-fade self-stretch flex-1 overflow-y-auto -mx-2 py-1"
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
        {known != null && !hasJoined && !query && (
          // 4 px of room on each side: the list clips its overflow, and the button's ring is
          // drawn outside it.
          <div className="p-1">
            <Button
              type="empty"
              htmlType="button"
              iconLeft="globe"
              align="left"
              fillWidth
              onClick={() => {
                onClose();
                setView("browse");
              }}
            >
              Find public namespaces to join in Spaces
            </Button>
          </div>
        )}
        {groups.map((x) => (
          <GroupDisclosure
            key={x.g}
            group={x.g}
            {...groupMeta(x.g)}
            total={x.all.length}
            matched={x.rows.length}
            searching={!!q}
            expanded={x.expanded}
            tabIndex={tabIndexOf(`group:${x.g}`)}
            onToggle={() =>
              q
                ? setSearchOpen((o) => ({ ...o, [x.g]: !x.expanded }))
                : setOpenGroups((o) => ({ ...o, [x.g]: !x.expanded }))
            }
          >
            {x.g === "code" && <p className="px-2 py-1 text-xs text-text-muted">Code that Loom indexes, read-only.</p>}
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
                onClick={() => {
                  setShowAll((m) => ({ ...m, [x.g]: true }));
                  setFocusNav(`ns:${x.rows[x.shown.length].namespace}`);
                }}
              >
                {`Show all ${x.rows.length}`}
              </Button>
            )}
          </GroupDisclosure>
        ))}
      </div>

      <div className="self-stretch flex flex-col items-stretch border-t border-border pt-2 -mx-2">
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
        {known != null && (hasJoined || !!query) && (
          <Button
            type="empty"
            htmlType="button"
            iconLeft="globe"
            align="left"
            onClick={() => {
              onClose();
              setView("browse");
            }}
          >
            Browse and join spaces
          </Button>
        )}
        <Button type="empty" htmlType="button" iconLeft="plus" align="left" onClick={onCreate}>
          Create namespace
        </Button>
        <Button
          type="empty"
          htmlType="button"
          iconLeft="folder-open"
          align="left"
          onClick={() => {
            onClose();
            setView("knowledge");
          }}
        >
          Open collections in the Library
        </Button>
        {/* One line: a taller footer shrinks the list and gives it a scrollbar. The button above
            says where collections are. */}
        <p className="px-3 pt-1 text-xs text-text-muted">A namespace is a project's or an agent's memory.</p>
      </div>
    </>
  );
}

type KindGroup = "code" | "tracker";
const KIND_GROUPS: KindGroup[] = ["code", "tracker"];
const GROUP_META: Record<string, { title: string; icon: IconName }> = {
  code: { title: "Repositories", icon: "code" },
  tracker: { title: "Tracker records", icon: "flag" },
};
// An open group shows this many rows, then "Show all N".
const GROUP_CAP = 8;

/** The folded group a row belongs in, from the server's `kind`; a collection is set apart
 *  before this by its reserved `col-` prefix. */
function kindGroup(r: MyNamespaceEntry | undefined): KindGroup | null {
  if (r?.kind === "code") return "code";
  if (r?.kind === "qa" || r?.kind === "tracker") return "tracker";
  return null;
}

/** A group's title and icon: a kind's own, or a name family's shared prefix ("team-…"). */
function groupMeta(group: string): { title: string; icon: IconName } {
  return GROUP_META[group] ?? { title: `${group}…`, icon: "folder-closed" };
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
  title,
  icon,
  total,
  matched,
  searching,
  expanded,
  tabIndex,
  onToggle,
  children,
}: {
  group: string;
  title: string;
  icon: IconName;
  total: number;
  matched: number;
  searching: boolean;
  expanded: boolean;
  tabIndex: number;
  onToggle: () => void;
  children: ReactNode;
}) {
  const id = useId();
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
        <Icon name={icon} size="sm" aria-hidden="true" />
        <span id={`${id}-label`} className="min-w-0 truncate font-medium">
          {title}
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
  /** A grouped row (a code index, a tracker record, a member of a name family): its name is
   *  an identifier, in code, on the second line under its description when it has one. */
  kindRow?: boolean;
}) {
  const active = r.namespace === current;
  const pendingRole = r.pending_role;
  const name = kindRow ? <code className="font-mono">{r.namespace}</code> : r.namespace;
  const [first, second] = kindRow && r.description ? [r.description, name] : [name, r.description];
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
          <span className="text-sm truncate block">{first}</span>
          {second && <span className="text-xs text-text-muted truncate block">{second}</span>}
        </span>
        {r.is_new && <Badge text="New" state="info" size="sm" />}
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
            // 4 px of room on its right for its focus ring: the list clips its overflow.
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
    return <Icon name="star" size="xs" color="var(--color-text-muted)" aria-label="You're admin" />;
  }
  return <Icon name="user" size="xs" color="var(--color-text-muted)" aria-label={role} />;
}

function VisibilityBadge({ v }: { v: MyNamespaceEntry["visibility"] }) {
  if (v === "public") {
    return <Icon name="globe" size="xs" color="var(--color-text-muted)" aria-label="Public" />;
  }
  return <Icon name="lock" size="xs" color="var(--color-text-muted)" aria-label="Private" />;
}
