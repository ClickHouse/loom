import { lazy, Suspense, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";

// The dialog loads on the first press: an empty page carries only the button.
const ConnectAgentDialog = lazy(() => import("./ConnectAgentDialog").then((m) => ({ default: m.ConnectAgentDialog })));

/**
 * ConnectAgentButton — "Connect an agent" and the dialog it opens (`ConnectAgentDialog`):
 * one line in an empty state. Closing returns focus here: `ui/Modal` hands it back to the
 * control that opened it.
 */
export function ConnectAgentButton({ type = "secondary" }: { type?: "primary" | "secondary" }) {
  const [open, setOpen] = useState(false);
  // Mounted from the first press on, so a close runs through the dialog rather than unmounting it.
  const [opened, setOpened] = useState(false);
  return (
    <>
      <Button
        type={type}
        htmlType="button"
        onClick={() => {
          setOpened(true);
          setOpen(true);
        }}
      >
        Connect an agent
      </Button>
      {opened && (
        <Suspense fallback={null}>
          <ConnectAgentDialog open={open} onOpenChange={setOpen} />
        </Suspense>
      )}
    </>
  );
}
