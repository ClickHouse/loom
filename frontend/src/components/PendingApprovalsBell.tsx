import { lazy, Suspense, useEffect, useState } from "react";
import { Popover } from "@clickhouse/click-ui/Popover";
import { IconButton } from "./IconButton";
import { listPendingApprovals } from "../api";
import { sharedCodeRepos } from "../lib/sharedReads";
import { useVisiblePoll } from "../lib/hooks";
import { useAfterPaint } from "../lib/afterPaint";
import { useStore } from "../lib/state";
import { canCallCodeRepos } from "../lib/canCall";
import { PopoverTrigger } from "./ui/Menu";
import { CountBadge } from "./CountBadge";
import type { AdminPending } from "./PendingApprovalsList";
const PendingApprovalsList = lazy(() =>
  import("./PendingApprovalsList").then((m) => ({ default: m.PendingApprovalsList })),
);

export function PendingApprovalsBell() {
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const [items, setItems] = useState<AdminPending[]>([]);
  const [open, setOpen] = useState(false);
  // The list's chunk loads on the first open; the members dialog on a row's click.
  const [opened, setOpened] = useState(false);

  // One org-level call answers "what can I approve", instead of namespace.list_mine and
  // then grants.list once per administered namespace on every poll in every tab. The
  // server already excludes the caller's own requests (they live in
  // PendingRequestBanner).
  const refresh = async () => {
    if (!identity) return;
    try {
      const r = await listPendingApprovals(identity.org, identity.namespace);
      const collected: AdminPending[] = (r.rows || []).map((g) => ({
        namespace: g.namespace,
        kind: g.kind,
        subject: g.subject,
        requested_role: g.requested_role,
        current_role: g.current_role,
        pending_since: g.pending_since,
      }));
      setItems(collected);
    } catch {
      /* fail-quiet */
    }
  };

  // Branches of one repository are one entry per person. Only a caller with standing on a
  // code namespace (or an admin claim) asks for the repositories; anyone else keeps the
  // namespace identifier as the label and sends nothing.
  const myNamespaces = useStore((s) => s.myNamespaces);
  const reposReadable = canCallCodeRepos(myNamespaces, whoami);
  const [repoInfo, setRepoInfo] = useState<Record<string, { repo: string; branch: string }>>({});
  // The repositories are asked for only when there is a code-namespace request to name,
  // not on every page load for a popover that is usually empty and closed.
  const hasCodeItems = items.some((it) => it.kind === "code");
  useEffect(() => {
    if (!identity?.org || !reposReadable || !hasCodeItems) return;
    sharedCodeRepos(identity.org)
      .then((rows) =>
        setRepoInfo(Object.fromEntries(rows.map((r) => [r.namespace, { repo: r.repo, branch: r.branch }]))),
      )
      .catch(() => {});
  }, [identity?.org, reposReadable, hasCodeItems]);
  // The read is org-scoped: a namespace switch changes nothing here. Every
  // two minutes while the tab is visible; a hidden tab catches up on return.
  // The first read leaves after the page's first paint, on idle — an approvals count is
  // not what the person is waiting for.
  useAfterPaint(
    () => {
      void refresh();
    },
    [identity?.org, whoami?.subject],
    !!identity,
  );
  useVisiblePoll(refresh, 120_000, !!identity);

  if (!identity) return null;

  const tip =
    items.length > 0 ? `${items.length} pending approval${items.length === 1 ? "" : "s"}` : "No pending approvals";
  return (
    <Popover
      open={open}
      onOpenChange={(v) => {
        setOpen(v);
        if (v) setOpened(true);
      }}
    >
      <span className="relative inline-flex">
        <PopoverTrigger>
          <IconButton
            icon="bell"
            label={items.length > 0 ? `Pending approvals, ${items.length}` : "Pending approvals"}
            tooltip={tip}
          />
        </PopoverTrigger>
        {items.length > 0 && <CountBadge count={items.length} />}
      </span>
      {opened && (
        <Suspense fallback={null}>
          <PendingApprovalsList items={items} repoInfo={repoInfo} refresh={refresh} close={() => setOpen(false)} />
        </Suspense>
      )}
    </Popover>
  );
}
