import { lazy, Suspense, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";

// The dialog loads on the first press: an empty page carries only the button.
const ConnectAgentDialog = lazy(() => import("./ConnectAgentDialog").then((m) => ({ default: m.ConnectAgentDialog })));

/**
 * ConnectAgentButton — "Connect an agent" and the dialog it opens (`ConnectAgentDialog`):
 * one line in an empty state. Closing hands focus back to this button.
 */
export function ConnectAgentButton({ type = "secondary" }: { type?: "primary" | "secondary" }) {
  const [open, setOpen] = useState(false);
  // Mounted from the first press on, so the dialog's close runs and hands focus back.
  const [opened, setOpened] = useState(false);
  const ref = useRef<HTMLButtonElement | null>(null);
  return (
    <>
      <Button
        ref={ref}
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
          <ConnectAgentDialog
            open={open}
            onOpenChange={setOpen}
            onCloseAutoFocus={(e) => {
              if (!ref.current?.isConnected) return;
              e.preventDefault();
              ref.current.focus();
            }}
          />
        </Suspense>
      )}
    </>
  );
}
