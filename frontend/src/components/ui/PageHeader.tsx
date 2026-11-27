import { useEffect, type ReactNode } from "react";

/**
 * The page heading, scope and actions. The title stays compact on a phone so the
 * workspace's contents remain visible when there are several actions.
 */
export interface PageHeaderProps {
  title: string;
  /** What the route shows inside the page (a tab, as Memories' Recall, or the Library's
   *  place), so the document title names it. */
  tab?: string;
  /** Beside the title: the scope badge, a status badge, a filter that belongs to it. */
  children?: ReactNode;
  /** A short description of what the page can do. */
  description?: ReactNode;
  /** The page's actions, at the end of the row. */
  actions?: ReactNode;
}

export function PageHeader({ title, tab, children, description, actions }: PageHeaderProps) {
  // Each page titles the document after itself (WCAG 2.4.2), so a screen reader announces
  // the page a navigation reaches.
  useEffect(() => {
    document.title = tab ? `${tab} · ${title} · Loom` : `${title} · Loom`;
    return () => {
      document.title = "Loom";
    };
  }, [title, tab]);
  return (
    <div data-page-header="" className="workspace-page-header mb-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h1 className="min-w-0 wrap-anywhere">{title}</h1>
        {children}
        {actions && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {description && <div className="workspace-page-description hidden sm:block">{description}</div>}
    </div>
  );
}
