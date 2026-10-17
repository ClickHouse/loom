import { Alert } from "./ui/Alert";
import { Button } from "@clickhouse/click-ui/Button";
import { useBuildWatch } from "../lib/buildWatch";

/** "A new version is live — Reload." Shown once the server's build differs
 * from the one this tab booted against. */
export function ReloadBar({ enabled = true }: { enabled?: boolean }) {
  const newBuild = useBuildWatch(enabled);
  if (!newBuild) return null;
  return (
    <div role="status" data-testid="reload-bar">
      <Alert
        type="banner"
        state="info"
        customIcon="refresh"
        text={
          <span className="flex flex-wrap items-center justify-center gap-3">
            <span>A new version of Loom is live (build {newBuild.slice(0, 9)}). Reload to pick it up.</span>
            <Button type="secondary" htmlType="button" iconLeft="refresh" onClick={() => window.location.reload()}>
              Reload
            </Button>
          </span>
        }
      />
    </div>
  );
}
