import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { readBackLabel } from "../lib/place";

/**
 * "Back to memory api/rate-limit": the way back a cross-view jump offers at the top of the
 * page it lands on. The jump names what it left in the new history entry
 * (`navigate(v, { back })`); this link goes back to that entry, which restores the scroll,
 * the open drawer and the focus. Read on every render: a walk taken on the page is a new
 * entry without a way back, and the link goes with it.
 */
export function BackLink() {
  const label = readBackLabel();
  if (!label) return null;
  return (
    <div>
      <Link component="button" type="button" size="sm" onClick={() => history.back()} data-testid="back-link">
        <Icon name="arrow-left" size="sm" aria-hidden="true" />
        Back to {label}
      </Link>
    </div>
  );
}
