import type { ReactNode } from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";

type ExternalLinkProps = {
  href: string;
  className?: string;
} & (
  | {
      children: ReactNode;
      /** The surrounding text's size: md is 14 px (`text-sm`), sm is 12 px (`text-xs`). */
      size?: "sm" | "md";
      label?: never;
    }
  | {
      children?: never;
      /** The name of the icon-only link. */
      label: string;
      size?: never;
    }
);

/**
 * ExternalLink — a Click UI `Link` that opens in a new tab and says so. Click UI's own
 * `icon` prop names its icon ("popout"), which a screen reader would read after the
 * link text; this draws the same icon hidden and adds "(opens in a new tab)". With no
 * children it is the icon alone, named by `label`, on a 24 px target (Click UI has no
 * icon link).
 */
export function ExternalLink(props: ExternalLinkProps) {
  const { href, className = "" } = props;
  if (props.children === undefined) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        aria-label={`${props.label} (opens in a new tab)`}
        className={`inline-flex items-center justify-center shrink-0 min-h-6 min-w-6 rounded text-text-muted hover:text-text hover:bg-surface-3 pointer-coarse:min-h-11 pointer-coarse:min-w-11 ${className}`}
      >
        <Icon name="popout" size="xs" aria-hidden="true" />
      </a>
    );
  }
  // `inline`, not Click UI's inline-flex: a URL that wraps keeps the icon after its last
  // character instead of beside the middle line.
  return (
    <Link href={href} target="_blank" rel="noreferrer" size={props.size ?? "md"} className={`inline ${className}`}>
      {props.children} <span className="sr-only">(opens in a new tab)</span>
      <Icon name="popout" size="xs" aria-hidden="true" className="inline-block align-middle ml-1" />
    </Link>
  );
}
