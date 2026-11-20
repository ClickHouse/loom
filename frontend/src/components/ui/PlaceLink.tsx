import { forwardRef, type ComponentPropsWithoutRef, type MouseEvent, type ReactNode } from "react";
import { Link } from "@clickhouse/click-ui/Link";
import { isModifiedClick } from "../../lib/graphLink";
import { placeQuery } from "../../lib/libraryPlace";
import { navigationHref } from "../../lib/state";

/** A link to a place in the Library (a collection, All documents as `ALL_DOCUMENTS`, or null
 *  for the header's namespace): a plain click opens it here, a modified one (a new tab or
 *  window) is the browser's. It takes a ref and other props, so a tooltip can wrap it.
 *  `plain`: a bare anchor its class draws (a tree row, a chip), not Click UI's Link. */
export const PlaceLink = forwardRef<
  HTMLAnchorElement,
  { collection: string | null; onOpen: () => void; plain?: boolean; children: ReactNode } & Omit<
    ComponentPropsWithoutRef<"a">,
    "href" | "children"
  >
>(function PlaceLink({ collection, onOpen, plain = false, children, onClick, ...rest }, ref) {
  const props = {
    ...rest,
    ref,
    href: navigationHref("knowledge", placeQuery(collection)),
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
