/**
 * Where a collection sits, on the Library: the path to it from the top on one line above the
 * documents (the parent a link, the ones above it folded; on a phone, one button back to the
 * parent), the collections inside the one shown as folder chips on that line, and on "My
 * files" and on a namespace the caller's own top-level collections and those shared with
 * them, listed below the documents where the tree is not shown. The hierarchy is the
 * server's (`collection.list` gives each collection's parent); in the path, a parent the
 * caller cannot read is a dots button, never a link, and gives its address when tapped.
 */
import {
  type ComponentProps,
  type ComponentPropsWithoutRef,
  forwardRef,
  type MouseEvent,
  type ReactNode,
  type RefObject,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { Popover } from "@clickhouse/click-ui/Popover";
import type { CollectionEntry } from "../api";
import { IconButton } from "../components/IconButton";
import { PopoverTrigger } from "../components/ui/Menu";
import { CHIP_OUTLINE_CLASS } from "../components/ui/Chip";
import { SectionHeading } from "../components/ui/SectionHeading";
import { TextClamp, useClamped } from "../components/ui/TextClamp";
import { Tip } from "../components/ui/Tooltip";
import { COLLECTIONS_LISTED, collectionOrder, collectionTitle, distinctTitle, roleLabel } from "../lib/collections";
import { fmtInt } from "../lib/format";
import { useLastPageFocus } from "../lib/useLastPageFocus";
import { isModifiedClick } from "../lib/graphLink";
import { navigationHref } from "../lib/state";

const HEADING_ID = "library-top-level";
const SHARED_HEADING_ID = "library-collections-shared";

/** Places listed at first, and how many more each "Show more" adds. */
const FIRST_SHOWN = 6;
export const MORE_SHOWN = 50;

/** An ancestor missing from the list: its address, all `collection.list` says of it. */
export interface HiddenPart {
  hidden: string;
}

/** The path from the top to `current`: each readable ancestor, and the first one the caller
 *  cannot read as a `HiddenPart` (the path stops there). */
export function collectionPath(
  current: CollectionEntry,
  byName: Map<string, CollectionEntry>,
): (CollectionEntry | HiddenPart)[] {
  const path: (CollectionEntry | HiddenPart)[] = [current];
  let parent = current.parent;
  // A cycle (pages of the list read from replicas that disagree) stops where it repeats.
  while (parent && !path.some((x) => "namespace" in x && x.namespace === parent)) {
    const p = byName.get(parent);
    if (!p) {
      path.unshift({ hidden: parent });
      break;
    }
    path.unshift(p);
    parent = p.parent;
  }
  return path;
}

/** Where a walk no root reaches (a cycle) starts for `c`: the top of its path, where the
 *  cycle repeats, so a child of a cycle stays under its parent. */
export function cycleStart(c: CollectionEntry, byName: Map<string, CollectionEntry>): CollectionEntry {
  const top = collectionPath(c, byName)[0];
  return "namespace" in top ? top : c;
}

/** A collection someone shared with the caller: one they read or write but do not
 *  administer. `collection.list` names no owner, so the role is the signal: one shared as
 *  admin is the caller's to manage, and sits with their own. The role is the token's, so a
 *  token capped below admin lists the person's own collections here too. */
export function isSharedWithMe(c: CollectionEntry): boolean {
  return !c.personal && c.role !== "admin";
}

/** The caller's own top-level collections (and their own whose parent they cannot read). */
export function topLevelCollections(rows: CollectionEntry[]): CollectionEntry[] {
  const names = new Set(rows.map((c) => c.namespace));
  return rows
    .filter((c) => !c.personal && !isSharedWithMe(c) && (!c.parent || !names.has(c.parent)))
    .sort(collectionOrder);
}

/** The collections directly inside `c`. */
export function childrenOf(rows: CollectionEntry[], c: CollectionEntry): CollectionEntry[] {
  // My files stays apart, as in the tree, even under a parent.
  return rows.filter((r) => r.parent === c.namespace && !r.personal).sort(collectionOrder);
}

/** "Shared with you", at the top level: each collection shared with the caller whose parent
 *  is not itself shared with them (a shared one's children are inside it), wherever it sits. */
export function sharedWithMe(rows: CollectionEntry[]): CollectionEntry[] {
  const shared = new Set(rows.filter(isSharedWithMe).map((c) => c.namespace));
  return rows.filter((c) => shared.has(c.namespace) && !shared.has(c.parent)).sort(collectionOrder);
}

/** Where a collection sits, by its parents from the top ("in Team › Research"); a parent the
 *  caller cannot read is "…", and alone is said in words, as the path's dots button says it.
 *  Empty for a top-level one. */
function pathText(
  c: CollectionEntry,
  byName: Map<string, CollectionEntry>,
  repeated: Set<string>,
  truncated: boolean,
): string {
  const parents = collectionPath(c, byName).slice(0, -1);
  if (parents.length === 0) return "";
  if (parents.length === 1 && "hidden" in parents[0]) return `inside ${hiddenWhat(truncated)}`;
  return `in ${parents.map((p) => ("hidden" in p ? "…" : distinctTitle(p, repeated))).join(" › ")}`;
}

/** The place picker's name, shown where no tree is; the tree hands it the focus when it leaves. */
export const PLACE_PICKER_LABEL = "Show the documents of";

/** When `box`'s element leaves with the focus inside it (a resize swaps the tree and the
 *  picker), `target` takes it on the next frame, once the other is drawn. Only focus that fell
 *  to the page moves: a navigation away places its own. */
export function useFocusHandoff(box: RefObject<HTMLElement | null>, target: () => HTMLElement | null): void {
  const latest = useRef(target);
  latest.current = target;
  useLayoutEffect(() => {
    const el = box.current;
    return () => {
      if (!el?.contains(document.activeElement)) return;
      requestAnimationFrame(() => {
        if (document.activeElement && document.activeElement !== document.body) return;
        latest.current()?.focus();
      });
    };
  }, [box]);
}

/** The place above the top: its label, the collection it is (null for the header's
 *  namespace) and how to open it. */
export interface Home {
  label: string;
  collection: string | null;
  open: () => void;
}

/** A link to a place in the Library: a plain click opens it here, a modified one (a new
 *  tab or window) is the browser's. It takes a ref and other props, so a tooltip can wrap it.
 *  `plain`: a bare anchor its class draws (a tree row, a chip), not Click UI's Link. */
const PlaceLink = forwardRef<
  HTMLAnchorElement,
  { collection: string | null; onOpen: () => void; plain?: boolean; children: ReactNode } & Omit<
    ComponentPropsWithoutRef<"a">,
    "href" | "children"
  >
>(function PlaceLink({ collection, onOpen, plain = false, children, onClick, ...rest }, ref) {
  const props = {
    ...rest,
    ref,
    href: navigationHref("knowledge", collection ? { collection } : {}),
    onClick: (e: MouseEvent<HTMLAnchorElement>) => {
      // A wrapping tooltip trigger's handler runs first (it closes the tip): it does nothing
      // once the click's default is prevented.
      onClick?.(e);
      if (isModifiedClick(e)) return;
      e.preventDefault();
      onOpen();
    },
  };
  return plain ? <a {...props}>{children}</a> : <Link {...props}>{children}</Link>;
});

/** A place's link by its title (in the tree, the chips and the path), the title cut to its
 *  box: when cut, the whole title is a tooltip on the link (a keyboard focus opens it too);
 *  the whole title is the link's name either way. */
export function PlaceTitleLink({
  collection,
  title,
  icon,
  titleClassName = "min-w-0 flex-1 truncate",
  plain = false,
  onOpen,
  ...rest
}: {
  collection: string | null;
  title: string;
  icon?: ComponentProps<typeof Icon>["name"];
  /** The title's box, where it is cut. */
  titleClassName?: string;
  /** An anchor its class draws (a tree row, a chip); else Click UI's Link (the path). */
  plain?: boolean;
  onOpen: () => void;
} & Omit<ComponentPropsWithoutRef<"a">, "href" | "children" | "onClick" | "title">) {
  const [el, setEl] = useState<HTMLSpanElement | null>(null);
  const cut = useClamped(el, title);
  return (
    <Tip content={title} disabled={!cut}>
      <PlaceLink {...rest} collection={collection} onOpen={onOpen} plain={plain}>
        {icon && <Icon name={icon} size="sm" aria-hidden />}
        <span ref={setEl} className={titleClassName}>
          {title}
        </span>
      </PlaceLink>
    </Tip>
  );
}

/** A path part's title: one line, cut at 12rem. */
const PATH_TITLE = "inline-block max-w-48 truncate align-bottom";

/** A parent missing from the list, in words: past the list's end, or one the caller cannot read. */
function hiddenWhat(truncated: boolean): string {
  return truncated ? "a collection not listed here" : "a collection you can't open";
}

/** What a parent missing from the list is, by its address. */
function hiddenText(part: HiddenPart, truncated: boolean): string {
  return truncated
    ? `${part.hidden}: not listed here, since the list stops at ${fmtInt(COLLECTIONS_LISTED)} collections.`
    : `${part.hidden}: a collection you can't open. Its owner can share it with you.`;
}

/** An ancestor the caller cannot open: a dots button whose popover gives its address. A
 *  popover, not a tooltip: a tap on a phone opens it. `lead` starts the button's name. */
function HiddenParent({ part, truncated, lead = "" }: { part: HiddenPart; truncated: boolean; lead?: string }) {
  const what = hiddenWhat(truncated);
  const label = lead ? `${lead}${what}` : what[0].toUpperCase() + what.slice(1);
  return (
    <Popover>
      <PopoverTrigger>
        <IconButton icon="dots-horizontal" size="xs" label={label} />
      </PopoverTrigger>
      <Popover.Content align="start" className="w-[min(320px,90vw)] text-sm">
        {hiddenText(part, truncated)}
      </Popover.Content>
    </Popover>
  );
}

/** The path on a phone: one button back to the parent (or to the place above the top),
 *  at the start of the select's row, so the documents still start on the first screen. */
export function CollectionBack({
  current,
  byName,
  truncated = false,
  home,
  onPick,
}: {
  current: CollectionEntry;
  /** Every collection the caller can read, by name. */
  byName: Map<string, CollectionEntry>;
  truncated?: boolean;
  home?: Home;
  onPick: (collection: string) => void;
}) {
  const path = collectionPath(current, byName);
  const up = path.length > 1 ? path[path.length - 2] : null;
  // A parent the person cannot open: back to the place that lists this one, if any.
  if (up && "hidden" in up && !home) return <HiddenParent part={up} truncated={truncated} lead="Back: " />;
  const back = up && !("hidden" in up) ? up : null;
  if (!back && !home) return null;
  return (
    <IconButton
      icon="chevron-left"
      size="xs"
      label={`Back to ${back ? collectionTitle(back) : home?.label}`}
      onClick={back ? () => onPick(back.namespace) : home?.open}
    />
  );
}

export function CollectionPath({
  current,
  byName,
  repeated,
  truncated = false,
  home,
  onPick,
  named = false,
}: {
  current: CollectionEntry;
  /** Every collection the caller can read, by name. */
  byName: Map<string, CollectionEntry>;
  /** Titles two collections share (`repeatedTitles`): those show their address too. */
  repeated: Set<string>;
  /** The list stopped early: a parent missing from it may be one the person reads. */
  truncated?: boolean;
  /** The place above the top that lists the top-level collections: its label and how to
   *  open it ("My files", or the header's namespace); none when there is no such place. */
  home?: Home;
  onPick: (collection: string) => void;
  /** The path ends with the current one's title (no select after it names it). */
  named?: boolean;
}) {
  const path = collectionPath(current, byName);
  // The ancestors only: the select after the path names the current one, or, beside the tree,
  // the path's last part does.
  const above = path.slice(0, -1);
  if (above.length === 0 && !home && !named) return null;
  // One line: the parent kept, the ones above it folded into a popover.
  const folded = above.slice(0, -1);
  const parent = above.at(-1);
  const sep = <Icon name="chevron-right" size="xs" aria-hidden="true" className="text-text-muted" />;
  return (
    <nav aria-label="Collection path" className="min-w-0 text-sm">
      <ol className="flex items-center gap-1">
        {home && (
          <li className="flex min-w-0 items-center gap-1">
            <PlaceTitleLink
              collection={home.collection}
              title={home.label}
              titleClassName={PATH_TITLE}
              onOpen={home.open}
            />
          </li>
        )}
        {folded.length > 0 && (
          <li className="flex items-center gap-1">
            {home && sep}
            <Popover>
              <PopoverTrigger>
                <IconButton icon="dots-horizontal" size="xs" label={`${folded.length} more above`} />
              </PopoverTrigger>
              <Popover.Content align="start" className="w-[min(320px,90vw)] text-sm">
                <ol className="space-y-1">
                  {folded.map((part) => (
                    <li key={"hidden" in part ? "hidden" : part.namespace}>
                      {"hidden" in part ? (
                        hiddenText(part, truncated)
                      ) : (
                        <PlaceLink collection={part.namespace} onOpen={() => onPick(part.namespace)}>
                          {distinctTitle(part, repeated)}
                        </PlaceLink>
                      )}
                    </li>
                  ))}
                </ol>
              </Popover.Content>
            </Popover>
          </li>
        )}
        {parent && (
          <li className="flex min-w-0 items-center gap-1">
            {(home || folded.length > 0) && sep}
            {"hidden" in parent ? (
              <HiddenParent part={parent} truncated={truncated} />
            ) : (
              <PlaceTitleLink
                collection={parent.namespace}
                onOpen={() => onPick(parent.namespace)}
                title={distinctTitle(parent, repeated)}
                titleClassName={PATH_TITLE}
              />
            )}
          </li>
        )}
        {named ? (
          // A breadcrumb's last step is the current page, as the tree's row is too.
          <li className="flex min-w-0 items-center gap-1">
            {(home || above.length > 0) && sep}
            <span aria-current="page" className="block min-w-0 max-w-48">
              <TextClamp text={distinctTitle(current, repeated)} reach="tooltip" />
            </span>
          </li>
        ) : (
          <li aria-hidden="true">{sep}</li>
        )}
      </ol>
    </nav>
  );
}

/** Below the documents on My files and on a namespace: the caller's own top-level
 *  collections, then those shared with them, each saying where it sits. */
export function TopLevelPlaces({
  rows,
  repeated,
  byName,
  truncated = false,
  onPick,
  className = "",
}: {
  rows: CollectionEntry[];
  /** Titles two collections share (`repeatedTitles`): those show their address too. */
  repeated: Set<string>;
  /** Every collection the caller can read, by name. */
  byName: Map<string, CollectionEntry>;
  /** The list stopped early: a parent missing from it may be one the caller can open. */
  truncated?: boolean;
  onPick: (collection: string) => void;
  className?: string;
}) {
  const own = useMemo(() => topLevelCollections(rows), [rows]);
  const shared = useMemo(() => sharedWithMe(rows), [rows]);
  if (own.length === 0 && shared.length === 0) return null;
  return (
    <div className={`space-y-6 ${className}`}>
      {/* Each says where it sits: one may be inside a collection not listed. */}
      <PlaceList
        id={HEADING_ID}
        label="Collections"
        items={own}
        repeated={repeated}
        byName={byName}
        onPick={onPick}
        truncated={truncated}
      />
      <PlaceList
        id={SHARED_HEADING_ID}
        label="Shared with you"
        items={shared}
        repeated={repeated}
        byName={byName}
        onPick={onPick}
        truncated={truncated}
      />
    </div>
  );
}

/** The links of a list of places, by the collection each row names (`data-collection`), for
 *  useLastPageFocus. */
function placeLinks(list: HTMLUListElement | null): Array<[string, HTMLElement]> {
  return [...(list?.querySelectorAll<HTMLLIElement>(":scope > li[data-collection]") ?? [])].flatMap<
    [string, HTMLElement]
  >((li) => {
    const link = li.querySelector<HTMLElement>("a");
    return link ? [[li.dataset.collection ?? "", link]] : [];
  });
}

/** The first `first` places, each Show more adding MORE_SHOWN; `extra` (one of `items`, on the
 *  way to the place shown) is drawn past them wherever it sits. `more` is the button's label,
 *  null once all are shown. The last press removes its button: the focus goes to the first
 *  place it added, else the last row shown, else `fallback`. */
export function useShowMore(
  items: CollectionEntry[],
  first: number,
  extra: CollectionEntry | null = null,
  fallback: () => HTMLElement | null = () => null,
) {
  const [count, setCount] = useState(first);
  const list = useRef<HTMLUListElement | null>(null);
  const shownAt = (n: number) => {
    const head = items.slice(0, n);
    return extra && !head.includes(extra) ? [...head, extra] : head;
  };
  const shown = shownAt(count);
  // What the next press adds: `extra` may already be drawn among it.
  const adds = shownAt(count + MORE_SHOWN).length - shown.length;
  const markShown = useLastPageFocus({
    loading: false,
    more: adds > 0,
    rows: () => placeLinks(list.current),
    fallback,
  });
  return {
    shown,
    list,
    more: adds > 0 ? `Show ${adds} more` : null,
    showMore: () => {
      markShown(shown.map((c) => c.namespace));
      setCount(count + MORE_SHOWN);
    },
    fewer: count > first,
    showFewer: () => setCount(first),
  };
}

/** One list of places to open, revealed a step at a time, each saying where it sits. */
function PlaceList({
  id,
  label,
  items,
  repeated,
  byName,
  onPick,
  truncated,
}: {
  id: string;
  label: string;
  items: CollectionEntry[];
  /** Titles shown twice, and every collection by name, over the whole list. */
  repeated: Set<string>;
  byName: Map<string, CollectionEntry>;
  onPick: (collection: string) => void;
  truncated: boolean;
}) {
  const { shown, list, more, showMore, fewer, showFewer } = useShowMore(items, FIRST_SHOWN, null, () =>
    document.getElementById(id),
  );
  if (items.length === 0) return null;
  // Links to other places, not rows of this list: a plain list, revealed a step at a time
  // (the select above finds any of them).
  return (
    <section aria-labelledby={id} className="space-y-1">
      <SectionHeading id={id} level={2} label={label} count={items.length} focusable />
      <ul ref={list} className="space-y-1">
        {shown.map((c) => {
          const path = pathText(c, byName, repeated, truncated);
          return (
            <li key={c.namespace} data-collection={c.namespace}>
              <div className="flex flex-wrap items-baseline gap-x-2">
                <PlaceLink collection={c.namespace} onOpen={() => onPick(c.namespace)}>
                  {distinctTitle(c, repeated)}
                </PlaceLink>
                <span className="text-xs text-text-muted">{roleLabel(c.role)}</span>
                {path && <span className="text-xs text-text-muted">{path}</span>}
              </div>
              {c.description && <TextClamp text={c.description} reach="tooltip" className="text-xs text-text-muted" />}
            </li>
          );
        })}
      </ul>
      <div className="flex gap-3">
        {more && (
          <Button type="empty" htmlType="button" onClick={showMore}>
            {more}
          </Button>
        )}
        {fewer && (
          <Button
            type="empty"
            htmlType="button"
            onClick={() => {
              // Its button goes with the list it shortens; the heading stays.
              document.getElementById(id)?.focus();
              showFewer();
            }}
          >
            Show fewer
          </Button>
        )}
      </div>
    </section>
  );
}

/** The collections inside the one shown, as link chips on the path's line; nothing at the top
 *  level, whose places the tree or the list below the documents shows. */
export function CollectionChips({
  rows,
  repeated,
  collection,
  onPick,
}: {
  /** Every collection the caller can read, or null while the list is read. */
  rows: CollectionEntry[] | null;
  /** Titles two collections share (`repeatedTitles`): those show their address too. */
  repeated: Set<string>;
  /** The collection shown; null on My files or a namespace. */
  collection: CollectionEntry | null;
  onPick: (collection: string) => void;
}) {
  const inside = useMemo(() => (rows && collection ? childrenOf(rows, collection) : []), [rows, collection]);
  const { shown, list, more, showMore } = useShowMore(inside, FIRST_SHOWN);
  if (!collection || inside.length === 0) return null;
  return (
    <nav aria-label={`Inside ${distinctTitle(collection, repeated)}`} className="library-chips">
      <ul ref={list}>
        {shown.map((c) => (
          <li key={c.namespace} data-collection={c.namespace}>
            <PlaceTitleLink
              collection={c.namespace}
              title={distinctTitle(c, repeated)}
              icon="folder-closed"
              plain
              className={`${CHIP_OUTLINE_CLASS} library-chip`}
              onOpen={() => onPick(c.namespace)}
            />
          </li>
        ))}
        {more && (
          <li>
            <Button type="empty" htmlType="button" onClick={showMore}>
              {more}
            </Button>
          </li>
        )}
      </ul>
    </nav>
  );
}
