import type { ReactNode } from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";

/**
 * ExternalLink — a Click UI `Link` that opens in a new tab and says so. Click UI's own
 * `icon` prop names its icon ("popout"), which a screen reader would read after the
 * link text; this draws the same icon hidden and adds "(opens in a new tab)".
 */
export function ExternalLink({
  href,
  children,
  size = "sm",
  className,
}: {
  href: string;
  children: ReactNode;
  size?: "xs" | "sm";
  className?: string;
}) {
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
