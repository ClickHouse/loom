import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { reloadPage } from "../lib/navigation";

/**
 * "Your session expired": the server refused the stored token mid-session. The page stays
 * as it is, so nothing typed into it is lost; signing in happens in a new tab, whose token
 * this tab then picks up (App listens for it). Reload signs in here instead.
 *
 * Pinned in the toast viewport (`ToastViewport`'s `pinned`): over every dialog, announced
 * while one is open, and a click on it does not dismiss the dialog.
 */
export function SessionExpiredBar() {
  return (
    <div role="alert" className="toast pointer-events-auto flex items-start" data-testid="session-expired-bar">
      <Icon name="lock" size="sm" aria-hidden="true" className="shrink-0 mt-0.5" />
      <span className="flex flex-col flex-1 min-w-0 gap-2">
        <span className="toast-title">Your session expired.</span>
        <span className="text-xs text-text-muted">
          Sign in again in a new tab and this page carries on as it is, or reload it.
        </span>
        <span className="flex flex-wrap gap-2">
          <Button
            type="primary"
            htmlType="button"
            iconLeft="lock"
            onClick={() => window.open("/", "_blank", "noopener")}
          >
            Sign in in a new tab
          </Button>
          <Button type="secondary" htmlType="button" iconLeft="refresh" onClick={reloadPage}>
            Reload
          </Button>
        </span>
      </span>
    </div>
  );
}
