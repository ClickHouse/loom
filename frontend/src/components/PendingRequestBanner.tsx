import { useEffect, useState } from "react";
import { Alert } from "./ui/Alert";
import { Button } from "@clickhouse/click-ui/Button";
import { cancelNamespaceRequest, type MyNamespaceEntry } from "../api";
import { sharedMine } from "../lib/sharedReads";
import { useStore } from "../lib/state";
import { toast, toastError } from "./Toast";
import { confirmAction } from "./ConfirmDialog";
import { IconButton } from "./IconButton";
import { relativeTime } from "../lib/format";

/**
 * Banner shown when the active namespace has a pending elevation
 * request for the current user. Surfaces who they're waiting on so
 * the "I can't write" 403 isn't mysterious.
 */
export function PendingRequestBanner() {
  const identity = useStore((s) => s.identity);
  const [entry, setEntry] = useState<MyNamespaceEntry | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    if (!identity) {
      setEntry(null);
      return;
    }
    let cancelled = false;
    sharedMine(identity.org)
      .then((rows) => {
        if (cancelled) return;
        const here = rows.find((r) => r.namespace === identity.namespace);
        setEntry(here && (here.pending_role || here.status === "pending") ? here : null);
      })
      .catch(() => setEntry(null));
    return () => {
      cancelled = true;
    };
    // A memory write cannot change membership — no memoriesVersion here.
  }, [identity?.org, identity?.namespace]);

  useEffect(() => {
    setDismissed(false);
  }, [identity?.namespace]);

  if (!entry || dismissed) return null;

  const requested = entry.pending_role || entry.role;
  const admins = entry.pending_admins || [];
  const since = entry.pending_since || entry.joined_at;

  const onCancel = async () => {
    if (!identity || !entry) return;
    if (
      !(await confirmAction({
        title: `Withdraw your ${requested} request on ${entry.namespace}?`,
        message: "Keeps your current access; the request just leaves the admin queue.",
        confirmLabel: "Withdraw",
      }))
    )
      return;
    setCancelling(true);
    try {
      await cancelNamespaceRequest(identity.org, entry.namespace);
      toast(`Withdrew ${requested} request on ${entry.namespace}`, "ok");
      setEntry(null);
    } catch (e) {
      toastError("Cancel failed", e);
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div className="px-6 xl:px-8 mt-4 relative">
      <Alert
        state="warning"
        size="medium"
        customIcon="clock"
        text={
          <div className="pr-8">
            <p>
              Your <strong>{requested}</strong> request on <span className="font-mono">{entry.namespace}</span> is
              awaiting approval. You can read in the meantime; writes are blocked until an admin approves.
            </p>
            {admins.length > 0 && (
              <p className="text-xs mt-1">
                Admins who can approve:{" "}
                {admins
                  .map((a) => (
                    <span key={a} className="font-mono">
                      {a}
                    </span>
                  ))
                  .reduce<React.ReactNode[]>((acc, el, i) => {
                    if (i > 0) acc.push(<span key={`sep-${i}`}>, </span>);
                    acc.push(el);
                    return acc;
                  }, [])}
                {since && <> · requested {relativeTime(since)}</>}
              </p>
            )}
            <div className="mt-2">
              <Button type="secondary" htmlType="button" onClick={onCancel} loading={cancelling}>
                Withdraw request
              </Button>
            </div>
          </div>
        }
      />
      <IconButton
        icon="cross"
        size="sm"
        label="Dismiss"
        onClick={() => setDismissed(true)}
        className="absolute top-2 right-8 xl:right-10"
      />
    </div>
  );
}
