import type { ReactNode } from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";

/**
 * ExternalLink — a Click UI `Link` that opens in a new tab and says so. Click UI's own
 * `icon` prop names its icon ("popout"), which a screen reader would read after the
 * link text; this draws the same icon hidden and adds "(opens in a new tab)". With no
 * children it is the icon alone, named by `label`, on a 24 px target (Click UI has no
 * icon link).
 */
export function ExternalLink({
  href,
  children,
  label,
  size = "md",
  className = "",
}: {
  href: string;
  children?: ReactNode;
  /** The name of an icon-only link. */
  label?: string;
  /** The surrounding text's size: md is 14 px (`text-sm`), sm is 12 px (`text-xs`). */
  size?: "sm" | "md";
  className?: string;
}) {
  if (children === undefined) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        aria-label={`${label} (opens in a new tab)`}
        className={`inline-flex items-center justify-center shrink-0 min-h-6 min-w-6 rounded text-text-muted hover:text-text hover:bg-surface-3 pointer-coarse:min-h-11 pointer-coarse:min-w-11 ${className}`}
      >
        <Icon name="popout" size="xs" aria-hidden="true" />
      </a>
    );
  }
  return (
    <Link href={href} target="_blank" rel="noreferrer" size={size} className={className}>
      {/* One flex item, so the space before the note survives. */}
      <span>
        {children} <span className="sr-only">(opens in a new tab)</span>
      </span>
      <Icon name="popout" size="xs" aria-hidden="true" />
    </Link>
  );
}
