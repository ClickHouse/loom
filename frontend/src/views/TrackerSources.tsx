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
import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import { useStore } from "../lib/state";
import { patchEntry } from "../lib/place";
import { useAbortableEffect } from "../lib/hooks";
import { humanizeError } from "../lib/errorWords";
import { forgetSavedNamespace, getSavedNamespace, listTrackerSources, listMine, setSavedNamespace } from "../api";
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
import { toast } from "../components/Toast";
import { RECORD_NOUN } from "../lib/repositoryCard";

export const TRACKER_SOURCES_ABSENT = "Trackers are not available on this deployment yet.";

/** The reader-grant sentence for a restricted namespace, said for a record. */
const PRIVATE_SOURCE_SENTENCE =
  "Private tracker. Request access asks its maintainers; you read nothing until one approves. One approval covers you as a person: every token minted for you, and the service accounts you create, read it without naming it.";

// One empty list, so a page that memoises on `sources` sees the same one each render.
const NO_SOURCES: TrackerSource[] = [];

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
    sources: status.kind === "ready" ? status.sources : NO_SOURCES,
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
  const navigate = useStore((s) => s.navigate);
  const [busy, setBusy] = useState<string | null>(null);
  // Back from a tracker's Patterns board: the entry Open left names the namespace it was
  // in (the switch made on it rewrote its address), and arriving on it switches back, once,
  // saving what Open decided (`open`); with nothing to save it forgets the pick Open made.
  useEffect(() => {
    const state = history.state as Record<string, unknown> | null;
    const left = state?.loomNamespaceLeft;
    if (typeof left !== "string") return;
    patchEntry({ loomNamespaceLeft: undefined, loomSavedLeft: undefined });
    const now = useStore.getState().identity;
    if (!now || now.namespace === left) return;
    setIdentity({ ...now, namespace: left });
    if (typeof state?.loomSavedLeft === "string") setSavedNamespace(state.loomSavedLeft);
    else {
      // A link opened the namespace Back returns to: it is the link's again, so the next
      // round from here saves it no more than this one did.
      forgetSavedNamespace();
      useStore.getState().setLinkOpened(left);
    }
    toast(`Switched back to ${left}`, "info");
  }, []);
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
  /** Open = the picker on that tracker namespace and the Graph view in Patterns mode, as
   *  a new history entry with a way back to Repositories (in the namespace it left). The
   *  namespace switch is said in a toast, so the side effect of the jump is never silent. */
  const open = (ns: TrackerSourceNamespace) => {
    if (!identity) return;
    // What Back saves: the pick Open replaces, else the namespace Open leaves, unless a link
    // opened that one for the tab (a link's namespace is never saved).
    const left = identity.namespace;
    const keep = getSavedNamespace() ?? (useStore.getState().linkOpened === left ? null : left);
    patchEntry({ loomNamespaceLeft: left, loomSavedLeft: keep });
    setIdentity({ ...identity, namespace: ns.namespace });
    setSavedNamespace(ns.namespace);
    navigate("graph", { query: { mode: "patterns" }, back: "Repositories" });
    toast(`Switched to ${ns.namespace}`, "info");
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
          Check, rotate or retire in Trackers
          <Icon name="arrow-down" size="xs" aria-hidden="true" className="ml-1" />
        </a>
      )}
    </div>
  );
}
