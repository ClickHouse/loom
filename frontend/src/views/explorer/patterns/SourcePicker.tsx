import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { listTrackerSources, setSavedNamespace } from "../../../api";
import { useAbortableEffect } from "../../../lib/hooks";
import { humanizeError } from "../../../lib/errorWords";
import { useStore } from "../../../lib/state";
import { resetNamespaceRoleCache } from "../../../lib/namespaceRole";
import {
  isOpAbsent,
  namespaceLabel,
  namespaceState,
  sortSources,
  type TrackerSource,
  type TrackerSourceNamespace,
} from "../../../lib/trackerSources";
import { Loading } from "../../../components/ui";

/**
 * SourcePicker — Patterns mode opened on a namespace that has no QA binding. The mode
 * stays (rather than dropping to the entity walk, which would make the page a QA-source
 * card opened look different when opened again from the sidebar), and the pane lists the
 * QA sources whose namespaces the caller already reads; choosing one switches the
 * picker's namespace (the QA-source card's own `open` rule) and Patterns loads. One
 * readable namespace is chosen without a click. None: a stated result that says where to
 * join one.
 */

/** The tracker namespaces the caller may read, source by source, in the Repositories order. */
export function readableTrackerNamespaces(
  sources: readonly TrackerSource[],
): Array<{ source: TrackerSource; ns: TrackerSourceNamespace }> {
  const out: Array<{ source: TrackerSource; ns: TrackerSourceNamespace }> = [];
  for (const source of sortSources(sources)) {
    for (const ns of source.namespaces) {
      if (namespaceState(ns) === "member") out.push({ source, ns });
    }
  }
  return out;
}

export function SourcePicker({
  org,
  current,
  onOpened,
}: {
  org: string;
  current: string;
  onOpened?: (namespace: string) => void;
}) {
  const identity = useStore((s) => s.identity);
  const setIdentity = useStore((s) => s.setIdentity);
  const [sources, setSources] = useState<TrackerSource[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [absent, setAbsent] = useState(false);

  useAbortableEffect(
    async (signal, live) => {
      setSources(null);
      setError(null);
      setAbsent(false);
      try {
        const r = await listTrackerSources(org, { signal });
        if (live()) setSources(r.sources || []);
      } catch (e) {
        if (!live()) return;
        if (isOpAbsent(e)) setAbsent(true);
        else setError(humanizeError(e, { read: true }));
        setSources([]);
      }
    },
    [org],
  );

  const open = (ns: TrackerSourceNamespace) => {
    if (!identity) return;
    setIdentity({ ...identity, namespace: ns.namespace });
    setSavedNamespace(ns.namespace);
    resetNamespaceRoleCache();
    onOpened?.(ns.namespace);
  };

  const readable = sources ? readableTrackerNamespaces(sources) : [];
  // One readable tracker namespace: open it — the picker is a question with one answer.
  useAbortableEffect(
    async (_signal, live) => {
      if (live() && sources && readable.length === 1 && readable[0].ns.namespace !== current) open(readable[0].ns);
    },
    [sources ? readable.map((r) => r.ns.namespace).join("|") : ""],
  );

  return (
    <section
      aria-labelledby="patterns-picker-title"
      className="flex-1 min-h-0 overflow-auto rounded-xl border border-border bg-surface p-5 max-w-2xl"
    >
      <h2 id="patterns-picker-title" className="text-text">
        Which tracker source?
      </h2>
      <p className="text-sm text-text-muted mt-1">
        Patterns reads a tracker namespace. <span className="font-mono">{current}</span> has none bound, so pick one you
        read; the page then stays on it.
      </p>
      {sources === null && <Loading layout="inline" label="Loading the tracker sources" className="mt-4" />}
      {absent && (
        <p role="status" className="text-sm text-text-muted mt-4">
          This server lists no tracker sources yet.
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-danger mt-4">
          {error}
        </p>
      )}
      {sources !== null && !absent && !error && readable.length === 0 && (
        <p role="status" className="text-sm text-text-muted mt-4">
          You read no tracker namespace yet. Join one from{" "}
          <a className="underline" href="/repositories">
            Repositories
          </a>{" "}
          (the tracker sources section); its card's Open lands here.
        </p>
      )}
      {readable.length > 1 && (
        <ul className="mt-4 divide-y divide-border" aria-label="tracker sources you read">
          {readable.map(({ source, ns }) => (
            <li key={ns.namespace} className="flex items-center justify-between gap-3 py-2">
              <div className="min-w-0">
                <div className="text-sm text-text truncate">{namespaceLabel(ns)}</div>
                <div className="text-xs text-text-muted font-mono truncate">
                  {source.name} · {ns.namespace}
                  {ns.classification === "restricted" ? " · restricted" : ""}
                </div>
              </div>
              <Button type="secondary" htmlType="button" className="shrink-0" onClick={() => open(ns)}>
                Open
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
