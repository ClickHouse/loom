import { useEffect, useState } from "react";
import { Clock as ClockIcon, X as XIcon } from "lucide-react";
import { cancelNamespaceRequest, type MyNamespaceEntry } from "../api";
import { sharedMine } from "../lib/sharedReads";
import { useStore } from "../lib/state";
import { toast } from "./Toast";
import { confirmAction } from "./ConfirmDialog";
import { relativeTime, humanizeError } from "../lib/format";

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
      toast(`Cancel failed: ${humanizeError(e)}`, "error");
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div className="px-6 xl:px-8 mt-4">
      <div className="rounded-lg border border-warn/60 bg-warn/10 px-4 py-3 flex items-start gap-3">
        <ClockIcon size={16} className="text-warn mt-0.5 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-warn">
            Your <strong>{requested}</strong> request on <span className="font-mono">{entry.namespace}</span> is
            awaiting approval. You can read in the meantime; writes are blocked until an admin approves.
          </div>
          {admins.length > 0 && (
            <div className="text-2xs text-warn/80 mt-1">
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
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={onCancel}
          disabled={cancelling}
          className="text-xs px-2.5 py-1 rounded-md border border-warn/60 text-warn hover:bg-warn/15 disabled:opacity-50 whitespace-nowrap"
        >
          {cancelling ? "Cancelling…" : "Withdraw request"}
        </button>
        <button
          type="button"
          onClick={() => setDismissed(true)}
          aria-label="Dismiss"
          className="p-1 text-warn hover:bg-warn/15 rounded"
        >
          <XIcon size={13} />
        </button>
      </div>
    </div>
  );
}
