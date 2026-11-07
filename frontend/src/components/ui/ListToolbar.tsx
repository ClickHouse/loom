import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import type { IconName } from "@clickhouse/click-ui/Icon";
import { MenuTrigger } from "./Menu";

export interface ListToolbarAction {
  label: string;
  icon: IconName;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
  loading?: boolean;
}

export interface ListToolbarProps {
  /** The bar's name. */
  label: string;
  /** The bar holds a search field, so it is a `search` landmark; false for a bar of filters,
   *  a time window or a count alone, which is a named group instead. */
  search?: boolean;
  /** The search field and the filters in force, on one line; none for a count alone. */
  children?: ReactNode;
  /** Where the list stands, at the end of the bar ("1–50 of 1,234"). */
  count?: ReactNode;
  /** While rows are selected: their number and what can be done to them. */
  selection?: {
    count: number;
    onClear: () => void;
    actions: ListToolbarAction[];
    /** The select-all checkbox, as Click UI's Table header has: every row on the page. */
    all?: { total: number; checked: boolean; onToggle: () => void };
  } | null;
}

/**
 * A list page's toolbar, the one element on the page that stays at the top as the list
 * scrolls. It is one fixed 48 px row (a 32 px Click UI field and 8 px around it): while
 * rows are selected, the selection and its actions take the search's place in the same
 * row, so the bar never grows over the rows beneath it. On a phone the actions fold into
 * one "Actions" menu. Its bottom edge shows only once it is stuck, Click UI's 1 px
 * separator. `main` keeps a matching scroll padding (styles.css), so a row reached with
 * Tab is never hidden under it.
 */
export function ListToolbar({ label, search = true, children, count, selection }: ListToolbarProps) {
  const sentinel = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(false);
  useEffect(() => {
    const el = sentinel.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([e]) => setStuck(!e.isIntersecting), { threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  // The selection's Clear button leaves with it (Clear itself, or a bulk action that empties
  // the selection): focus that fell to the page goes to the search the bar brings back.
  const bar = useRef<HTMLDivElement>(null);
  const Bar = search ? "search" : "div";
  const selecting = !!selection && selection.count > 0;
  const wasSelecting = useRef(selecting);
  useLayoutEffect(() => {
    if (wasSelecting.current && !selecting) {
      const lost = !document.activeElement || document.activeElement === document.body;
      if (lost) bar.current?.querySelector<HTMLElement>("input, button")?.focus();
    }
    wasSelecting.current = selecting;
  }, [selecting]);

  return (
    <>
      <div ref={sentinel} aria-hidden="true" className="h-px -mb-px" />
      <Bar
        ref={bar}
        role={search ? undefined : "group"}
        aria-label={label}
        data-stuck={stuck || undefined}
        className="list-toolbar sticky top-0 z-[2] -mx-2 mb-3 flex h-12 items-center gap-3 bg-bg px-2"
      >
        {selecting && selection ? (
          <>
            {selection.all && (
              <Checkbox
                checked={selection.all.checked ? true : "indeterminate"}
                onCheckedChange={selection.all.onToggle}
                aria-label={`Select all ${selection.all.total.toLocaleString()} on this page`}
              />
            )}
            {/* Said by the live region at the end of the bar. */}
            <span aria-hidden="true" className="text-sm font-medium tabular whitespace-nowrap">
              {selection.count} selected
            </span>
            <Button type="empty" htmlType="button" onClick={selection.onClear} aria-label="Clear selection">
              Clear
            </Button>
            <div className="ml-auto hidden items-center gap-2 sm:flex">
              {selection.actions.map((a) => (
                <Button
                  key={a.label}
                  type={a.danger ? "danger" : "secondary"}
                  htmlType="button"
                  iconLeft={a.icon}
                  disabled={a.disabled}
                  loading={a.loading}
                  onClick={a.onClick}
                >
                  {a.label}
                </Button>
              ))}
            </div>
            <div className="ml-auto sm:hidden">
              <Dropdown modal={false}>
                <MenuTrigger>
                  <Button type="secondary" htmlType="button" iconRight="chevron-down">
                    Actions
                  </Button>
                </MenuTrigger>
                <Dropdown.Content align="end">
                  {selection.actions.map((a) => (
                    <Dropdown.Item
                      key={a.label}
                      type={a.danger ? "danger" : "default"}
                      disabled={a.disabled || a.loading}
                      onSelect={a.onClick}
                    >
                      {a.label}
                    </Dropdown.Item>
                  ))}
                </Dropdown.Content>
              </Dropdown>
            </div>
          </>
        ) : (
          <div className="flex min-w-0 flex-1 items-center gap-2">{children}</div>
        )}
        {/* One live region, mounted with the bar, so each change is said: the count (shown on
            a phone too) as a search, a filter or a page moves it, or, unseen, the selection
            while rows are selected. */}
        <span
          aria-live="polite"
          aria-atomic="true"
          className={selecting ? "sr-only" : "shrink-0 whitespace-nowrap text-xs text-text-muted tabular"}
        >
          {selecting && selection ? `${selection.count} selected` : count}
        </span>
      </Bar>
    </>
  );
}

/** The toolbar's search field: it takes the room the filters beside it leave, and never
 *  less than 10rem. */
export function ListToolbarSearch({ children }: { children: ReactNode }) {
  return <div className="min-w-40 flex-1">{children}</div>;
}
