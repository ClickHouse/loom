import { Button } from "@clickhouse/click-ui/Button";
import { Alert } from "./ui/Alert";

/**
 * "Your session expired": the server refused the stored token mid-session. The page stays
 * as it is, so nothing typed into it is lost; signing in happens in a new tab, whose token
 * this tab then picks up (App listens for it). Reload signs in here instead.
 *
 * Fixed over the shell at the toasts' layer (z-[65], over every dialog), and it takes
 * pointer events back, which an open modal turns off on <body>.
 */
export function SessionExpiredBar() {
  return (
    <div role="alert" className="pointer-events-auto fixed inset-x-0 top-0 z-[65]" data-testid="session-expired-bar">
      <Alert
        type="banner"
        state="warning"
        customIcon="lock"
        text={
          <span className="flex flex-wrap items-center justify-center gap-3">
            <span>Your session expired. Sign in again in a new tab and this page carries on as it is.</span>
            <Button
              type="primary"
              htmlType="button"
              iconLeft="lock"
              onClick={() => window.open("/", "_blank", "noopener")}
            >
              Sign in in a new tab
            </Button>
            <Button type="secondary" htmlType="button" iconLeft="refresh" onClick={() => window.location.reload()}>
              Reload
            </Button>
          </span>
        }
      />
    </div>
  );
}
