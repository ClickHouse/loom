/**
 * Where a collection sits, on the Library: the path to it from the top, on one line before the
 * select (the parent a link, the ones above it folded; on a phone, one button back to the
 * parent), and the collections inside the one shown, or the top-level ones on "My files" and
 * on a namespace. The tree is the server's (`collection.list` gives each collection's
 * parent); in the path, a parent the caller cannot read is a dots button, never a link, and
 * gives its address when tapped.
 */
import {
  type ComponentPropsWithoutRef,
  forwardRef,
  type MouseEvent,
  type ReactNode,
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
import { SectionHeading } from "../components/ui/SectionHeading";
import { TextClamp, useClamped } from "../components/ui/TextClamp";
import { Tip } from "../components/ui/Tooltip";
import {
  COLLECTIONS_LISTED,
  collectionOrder,
  collectionTitle,
  distinctTitle,
  roleLabel,
  repeatedTitles,
} from "../lib/collections";
import { fmtInt } from "../lib/format";
import { useLastPageFocus } from "../lib/useLastPageFocus";
import { isModifiedClick } from "../lib/graphLink";
import { navigationHref } from "../lib/state";

const HEADING_ID = "library-collections-inside";

/** Collections inside listed at first, and how many more each "Show more" adds. */
const FIRST_INSIDE = 6;
const MORE_INSIDE = 50;

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

/** The collections listed under the place shown: the children of a collection, or the
 *  top-level ones (and those whose parent the caller cannot read) elsewhere. */
export function collectionsInside(rows: CollectionEntry[], current: CollectionEntry | null): CollectionEntry[] {
  const names = new Set(rows.map((c) => c.namespace));
  const under =
    current && !current.personal
      ? rows.filter((c) => c.parent === current.namespace)
      : rows.filter((c) => !c.personal && (!c.parent || !names.has(c.parent)));
  return under.sort(collectionOrder);
}

/** The place above the top: its label, the collection it is (null for the header's
 *  namespace) and how to open it. */
export interface Home {
  label: string;
  collection: string | null;
  open: () => void;
}

/** A link to a place in the Library: a plain click opens it here, a modified one (a new
 *  tab or window) is the browser's. It takes a ref and other props, so a tooltip can wrap it. */
const PlaceLink = forwardRef<
  HTMLAnchorElement,
  { collection: string | null; onOpen: () => void; children: ReactNode } & Omit<
    ComponentPropsWithoutRef<"a">,
    "href" | "onClick" | "children"
  >
>(function PlaceLink({ collection, onOpen, children, ...rest }, ref) {
  return (
    <Link
      {...rest}
      ref={ref}
      href={navigationHref("knowledge", collection ? { collection } : {})}
      onClick={(e: MouseEvent) => {
        if (isModifiedClick(e)) return;
        e.preventDefault();
        onOpen();
      }}
    >
      {children}
    </Link>
  );
});

/** A path title cut to one line: when cut, its whole text is a tooltip on the link (a
 *  keyboard focus opens it too); the whole text is the link's name either way. */
function CutLink({ collection, onOpen, text }: { collection: string | null; onOpen: () => void; text: string }) {
  const [el, setEl] = useState<HTMLSpanElement | null>(null);
  const cut = useClamped(el, text);
  return (
    <Tip content={text} disabled={!cut}>
      <PlaceLink collection={collection} onOpen={onOpen}>
        <span ref={setEl} className="inline-block max-w-48 truncate align-bottom">
          {text}
        </span>
      </PlaceLink>
    </Tip>
  );
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
  const what = truncated ? "a collection not listed here" : "a collection you can't open";
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
  rows,
  truncated = false,
  home,
  onPick,
}: {
  current: CollectionEntry;
  rows: CollectionEntry[];
  truncated?: boolean;
  home?: Home;
  onPick: (collection: string) => void;
}) {
  const byName = useMemo(() => new Map(rows.map((c) => [c.namespace, c])), [rows]);
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
  rows,
  truncated = false,
  home,
  onPick,
}: {
  current: CollectionEntry;
  rows: CollectionEntry[];
  /** The list stopped early: a parent missing from it may be one the person reads. */
  truncated?: boolean;
  /** The place above the top that lists the top-level collections: its label and how to
   *  open it ("My files", or the header's namespace); none when there is no such place. */
  home?: Home;
  onPick: (collection: string) => void;
}) {
  const byName = useMemo(() => new Map(rows.map((c) => [c.namespace, c])), [rows]);
  const repeated = useMemo(() => repeatedTitles(rows), [rows]);
  const path = collectionPath(current, byName);
  // The ancestors only: the select this path leads into names the current one.
  const above = path.slice(0, -1);
  if (above.length === 0 && !home) return null;
  // One line beside the select: the parent kept, the ones above it folded into a popover.
  const folded = above.slice(0, -1);
  const parent = above.at(-1);
  const sep = <Icon name="chevron-right" size="xs" aria-hidden="true" className="text-text-muted" />;
  return (
    <nav aria-label="Collection path" className="min-w-0 text-sm">
      <ol className="flex items-center gap-1">
        {home && (
          <li className="flex min-w-0 items-center gap-1">
            <CutLink collection={home.collection} onOpen={home.open} text={home.label} />
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
              <CutLink
                collection={parent.namespace}
                onOpen={() => onPick(parent.namespace)}
                text={distinctTitle(parent, repeated)}
              />
            )}
          </li>
        )}
        <li aria-hidden="true">{sep}</li>
      </ol>
    </nav>
  );
}

export function CollectionsInside({
  rows,
  current,
  onPick,
  className = "",
}: {
  rows: CollectionEntry[];
  current: CollectionEntry | null;
  onPick: (collection: string) => void;
  className?: string;
}) {
  const [count, setCount] = useState(FIRST_INSIDE);
  const list = useRef<HTMLUListElement | null>(null);
  const inside = useMemo(() => collectionsInside(rows, current), [rows, current]);
  // Over every collection listed, as the select and the path count them.
  const repeated = useMemo(() => repeatedTitles(rows), [rows]);
  const left = inside.length - count;
  // The last Show more removes its button: the focus goes to the first collection it added.
  const markShown = useLastPageFocus({
    loading: false,
    more: left > 0,
    rows: () =>
      [...(list.current?.querySelectorAll<HTMLLIElement>(":scope > li") ?? [])].flatMap((li) => {
        const link = li.querySelector<HTMLElement>("a");
        return link ? [[li.dataset.collection ?? "", link] as [string, HTMLElement]] : [];
      }),
    fallback: () => document.getElementById(HEADING_ID),
  });
  if (inside.length === 0) return null;
  // Links to other places, not rows of this list: a plain list, revealed a step at a time
  // (the select above finds any of them).
  return (
    <section aria-labelledby={HEADING_ID} className={`space-y-1 ${className}`}>
      <SectionHeading id={HEADING_ID} level={2} label="Collections" count={inside.length} focusable />
      <ul ref={list} className="space-y-1">
        {inside.slice(0, count).map((c) => (
          <li key={c.namespace} data-collection={c.namespace}>
            <div className="flex flex-wrap items-baseline gap-x-2">
              <PlaceLink collection={c.namespace} onOpen={() => onPick(c.namespace)}>
                {distinctTitle(c, repeated)}
              </PlaceLink>
              <span className="text-xs text-text-muted">{roleLabel(c.role)}</span>
            </div>
            {c.description && <TextClamp text={c.description} reach="tooltip" className="text-xs text-text-muted" />}
          </li>
        ))}
      </ul>
      <div className="flex gap-3">
        {left > 0 && (
          <Button
            type="empty"
            htmlType="button"
            onClick={() => {
              markShown(inside.slice(0, count).map((c) => c.namespace));
              setCount(count + MORE_INSIDE);
            }}
          >
            {`Show ${Math.min(left, MORE_INSIDE)} more`}
          </Button>
        )}
        {count > FIRST_INSIDE && (
          <Button
            type="empty"
            htmlType="button"
            onClick={() => {
              // Its button goes with the list it shortens; the heading stays.
              document.getElementById(HEADING_ID)?.focus();
              setCount(FIRST_INSIDE);
            }}
          >
            Show fewer
          </Button>
        )}
      </div>
    </section>
  );
}
