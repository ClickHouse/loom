/**
 * tracker sources — the record facet of a repository card.
 *
 * A tracker source is a database read live: issues, PRs, checks,
 * incidents. Its namespaces are what a person joins, through the same
 * `namespace.join` a repository's branches use, with the same standing words. A tracker
 * namespace is not a card of its own: it is the "Issues & PRs" facet of the repository
 * card its `repos[]` names (`lib/repositoryCard.ts`, `views/RepositoryCard.tsx`). This
 * module keeps what is specific to the source: the one read of `tracker.source.list` (on
 * a server without it, one stated line, never an error banner), the actions on a record
 * namespace (join, request, withdraw, leave through `lib/joinActions.ts`; Open = the
 * picker on that namespace and the Graph view in Patterns mode), and the diagnostics a
 * maintainer reads in the card's Details disclosure.
 *
 * The write ops (`tracker.source.add/check/rotate/remove`) reach the page — Register from
 * a repository card's maintainer panel, and Check / Rotate / Retire in the Details
 * disclosure for an admin of a registry source (`canAdminister`). The dialogs load on
 * first open.
 */
import { useCallback, useRef, useState } from "react";
import { useStore } from "../lib/state";
import { useAbortableEffect } from "../lib/hooks";
import { humanizeError } from "../lib/format";
import { listTrackerSources, listMine, setSavedNamespace } from "../api";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { joinTarget, withdrawTarget, leaveTarget, type JoinTarget } from "../lib/joinActions";
import {
  isOpAbsent,
  namespaceLabel,
  operatorOwned,
  sortSources,
  trackerAnchor,
  trackerStatus,
  type TrackerSource,
  type TrackerSourceNamespace,
} from "../lib/trackerSources";

import { MetaList, MetaRow, TextClamp } from "../components/ui";
import { confirmAction } from "../components/ConfirmDialog";
import { RECORD_NOUN } from "../lib/repositoryCard";

export const TRACKER_SOURCES_ABSENT = "Trackers are not available on this deployment yet.";

/** The reader-grant sentence for a restricted namespace, said for a record. */
const PRIVATE_SOURCE_SENTENCE =
  "Private tracker. Request access asks its maintainers; you read nothing until one approves. One approval covers you as a person: every token minted for you, and the service accounts you create, read it without naming it.";

type TrackerSourcesStatus =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "absent" }
  | { kind: "error"; message: string }
  | { kind: "ready"; sources: TrackerSource[] };

/**
 * One read of `tracker.source.list`, once the principal is known ( the op is org-level at
 * viewer role, so a known principal of the org may call it; with no whoami nothing is
 * requested). `refresh` re-reads after the caller's own action and re-reads
 * `namespace.list_mine` into the store so the picker and the role chips see the new
 * grant.
 */
export function useTrackerSources(org: string): {
  status: TrackerSourcesStatus;
  sources: TrackerSource[];
  selfService: boolean;
  refresh: (opts?: { fresh?: boolean }) => Promise<void>;
} {
  const whoami = useStore((s) => s.whoami);
  const setMyNamespaces = useStore((s) => s.setMyNamespaces);
  const [status, setStatus] = useState<TrackerSourcesStatus>({ kind: "idle" });
  const [selfService, setSelfService] = useState(false);
  const [nonce, setNonce] = useState(0);
  const freshNext = useRef(false);
  const may = !!whoami && !!org;

  useAbortableEffect(
    async (signal, live) => {
      if (!may) return;
      setStatus((s) => (s.kind === "ready" ? s : { kind: "loading" }));
      try {
        const fresh = freshNext.current;
        freshNext.current = false;
        const r = await listTrackerSources(org, { signal, fresh });
        if (!live()) return;
        setStatus({ kind: "ready", sources: sortSources(r.sources ?? []) });
        setSelfService(r.self_service);
      } catch (e) {
        if (!live()) return;
        if (isOpAbsent(e)) setStatus({ kind: "absent" });
        else setStatus({ kind: "error", message: humanizeError(e) });
      }
    },
    [org, may, nonce],
  );

  const refresh = useCallback(
    async (opts: { fresh?: boolean } = {}) => {
      if (opts.fresh) freshNext.current = true;
      setNonce((n) => n + 1);
      resetNamespaceRoleCache();
      try {
        setMyNamespaces(await listMine(org));
      } catch {
        /* the picker refetches on open */
      }
    },
    [org, setMyNamespaces],
  );

  return {
    status,
    sources: status.kind === "ready" ? status.sources : [],
    selfService: status.kind === "ready" && selfService,
    refresh,
  };
}

interface RecordActions {
  busy: string | null;
  open: (ns: TrackerSourceNamespace) => void;
  /** `confirm` — a chip confirms first; the header's Join button is the one direct verb. */
  join: (ns: TrackerSourceNamespace, confirm: boolean) => Promise<void>;
  request: (ns: TrackerSourceNamespace) => Promise<void>;
  cancel: (ns: TrackerSourceNamespace) => Promise<void>;
  leave: (ns: TrackerSourceNamespace) => Promise<void>;
}

/** Join, request, withdraw, leave and Open on a record namespace — the
 *  shared helpers in `lib/joinActions.ts`, then `refresh`. */
export function useRecordActions(org: string, refresh: () => Promise<void>): RecordActions {
  const identity = useStore((s) => s.identity);
  const setIdentity = useStore((s) => s.setIdentity);
  const setView = useStore((s) => s.setView);
  const [busy, setBusy] = useState<string | null>(null);
  const target = (ns: TrackerSourceNamespace): JoinTarget => ({
    org,
    namespace: ns.namespace,
    label: namespaceLabel(ns),
  });

  const join = async (ns: TrackerSourceNamespace, confirm: boolean) => {
    if (
      confirm &&
      !(await confirmAction({
        title: `Join the ${RECORD_NOUN}?`,
        message:
          "Open tracker — joins instantly, read-only. Agents you mint a token for can then ask about its issues, PRs and checks.",
        confirmLabel: "Join",
      }))
    )
      return;
    setBusy(ns.namespace);
    try {
      if (await joinTarget(target(ns), "viewer")) await refresh();
    } finally {
      setBusy(null);
    }
  };
  const request = async (ns: TrackerSourceNamespace) => {
    if (
      !(await confirmAction({
        title: `Request access to the ${RECORD_NOUN}?`,
        message: `${PRIVATE_SOURCE_SENTENCE} Everything on a tracker is read-only.`,
        confirmLabel: "Request access",
      }))
    )
      return;
    setBusy(ns.namespace);
    try {
      if (await joinTarget(target(ns), "viewer")) await refresh();
    } finally {
      setBusy(null);
    }
  };
  const cancel = async (ns: TrackerSourceNamespace) => {
    setBusy(ns.namespace);
    try {
      if (await withdrawTarget(target(ns))) await refresh();
    } finally {
      setBusy(null);
    }
  };
  const leave = async (ns: TrackerSourceNamespace) => {
    setBusy(ns.namespace);
    try {
      if (await leaveTarget(target(ns))) await refresh();
    } finally {
      setBusy(null);
    }
  };
  /** Open = the picker on that tracker namespace and the Graph view in Patterns
   * mode: the mode key is written into the current entry first, so the navigation carries
   *  it (`navigationHref` keeps `mode` for the graph view) and drops this page's `repo`.
   *  */
  const open = (ns: TrackerSourceNamespace) => {
    if (!identity) return;
    setIdentity({ ...identity, namespace: ns.namespace });
    setSavedNamespace(ns.namespace);
    try {
      history.replaceState(null, "", `${window.location.pathname}?mode=patterns`);
    } catch {
      /* history unavailable */
    }
    setView("graph");
  };
  return { busy, open, join, request, cancel, leave };
}

/**
 * A tracker's diagnostics in the card's Details disclosure: name, host, database, origin
 * and its state in plain words. Its admin verbs (Check now / Rotate / Retire) live in one
 * place — the Trackers list on the same page — and Details links there.
 */
export function RecordSourceDetails({
  source: s,
  manageable = false,
}: {
  source: TrackerSource;
  /** The caller administers it: link to its row in the Trackers list. */
  manageable?: boolean;
}) {
  const status = trackerStatus(s);
  return (
    // biome-ignore lint/a11y/useSemanticElements: a labelled block of read-only facts, not form fields
    <div role="group" aria-label={`Tracker ${s.name}`}>
      <MetaList>
        <MetaRow label="tracker" mono>
          {s.name}
        </MetaRow>
        <MetaRow label="host" mono>
          <TextClamp text={s.port ? `${s.host}:${s.port}` : s.host} reach="tooltip" mono />
        </MetaRow>
        <MetaRow label="database" mono>
          {s.database || "—"}
        </MetaRow>
        <MetaRow label="origin">{operatorOwned(s) ? "set up by the operator" : "added from the dashboard"}</MetaRow>
        <MetaRow
          label="state"
          tone={
            status.tone === "danger"
              ? "danger"
              : status.tone === "warn"
                ? "warn"
                : status.tone === "muted"
                  ? "muted"
                  : "default"
          }
        >
          {status.label} — {status.detail}
        </MetaRow>
      </MetaList>
      {manageable && (
        <a
          href={`#${trackerAnchor(s.name)}`}
          className="mt-1 inline-flex min-h-8 items-center text-xs pointer-coarse:min-h-11"
        >
          Check, rotate or retire in Trackers ↓
        </a>
      )}
    </div>
  );
}
