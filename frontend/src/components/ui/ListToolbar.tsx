import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
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
  /** The bar's name, as a `search` landmark. */
  label: string;
  /** The search field and the filters in force, on one line. */
  children: ReactNode;
  /** Where the list stands, at the end of the bar ("1–50 of 1,234"). */
  count?: ReactNode;
  /** While rows are selected: their number and what can be done to them. */
  selection?: { count: number; onClear: () => void; actions: ListToolbarAction[] } | null;
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
export function ListToolbar({ label, children, count, selection }: ListToolbarProps) {
  const sentinel = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(false);
  useEffect(() => {
    const el = sentinel.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([e]) => setStuck(!e.isIntersecting), { threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <>
      <div ref={sentinel} aria-hidden="true" className="h-px -mb-px" />
      <search
        aria-label={label}
        data-stuck={stuck || undefined}
        className="list-toolbar sticky top-0 z-[2] -mx-2 mb-3 flex h-12 items-center gap-3 bg-bg px-2"
      >
        {selection && selection.count > 0 ? (
          <>
            <span className="text-sm font-medium tabular whitespace-nowrap">{selection.count} selected</span>
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
                    <Dropdown.Item key={a.label} disabled={a.disabled || a.loading} onSelect={a.onClick}>
                      {a.label}
                    </Dropdown.Item>
                  ))}
                </Dropdown.Content>
              </Dropdown>
            </div>
          </>
        ) : (
          <>
            <div className="flex min-w-0 flex-1 items-center gap-2">{children}</div>
            {count && (
              <span className="hidden whitespace-nowrap text-xs text-text-muted tabular sm:inline">{count}</span>
            )}
          </>
        )}
      </search>
    </>
  );
}
