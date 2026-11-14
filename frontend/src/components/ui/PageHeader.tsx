import { useEffect, type ReactNode } from "react";

/**
 * PageHeader — the title row of every page: the h1 (20 px and no icon, like Click UI's
 * page titles), what qualifies it beside it (the ScopeBadge, a mode badge), and the page's
 * own actions at the end of the row. On a phone the row wraps.
 */
export interface PageHeaderProps {
  title: string;
  /** What the route shows inside the page (a tab, as Memories' Recall, or the Library's
   *  place), so the document title names it. */
  tab?: string;
  /** Beside the title: the scope badge, a status badge, a filter that belongs to it. */
  children?: ReactNode;
  /** The page's actions, at the end of the row. */
  actions?: ReactNode;
}

export function PageHeader({ title, tab, children, actions }: PageHeaderProps) {
  // Each page titles the document after itself (WCAG 2.4.2), so a screen reader announces
  // the page a navigation reaches.
  useEffect(() => {
    document.title = tab ? `${tab} · ${title} · Loom` : `${title} · Loom`;
    return () => {
      document.title = "Loom";
    };
  }, [title, tab]);
  return (
    <div data-page-header="" className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
      <h1>{title}</h1>
      {children}
      {actions && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
