/**
 * The Library's gaps tile: the questions people keep asking in this namespace that no
 * memory or document answered, grouped once a day. Admins only, since each one is
 * other people's questions; a question shows only once enough different people asked
 * it. Read once per namespace shown.
 */
import { useState } from "react";
import { listKnowledgeGaps, type KnowledgeGap } from "../api/knowledgeGaps";
import { ApiError } from "../lib/errors";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import { isOpAbsent } from "../lib/trackerSources";

type State =
  | { kind: "loading" }
  | { kind: "ready"; gaps: KnowledgeGap[] }
  | { kind: "not-capturing" }
  | { kind: "hidden" }
  | { kind: "error"; message: string };

export function KnowledgeGaps({ org, namespace }: { org: string; namespace: string }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useAbortableEffect(
    async (signal, live) => {
      setState({ kind: "loading" });
      try {
        const r = await listKnowledgeGaps({ org, namespace }, { signal });
        if (!live()) return;
        // Off by default: a deployment that records no misses shows no tile.
        if (!r.enabled) setState({ kind: "hidden" });
        else if (!r.capturing) setState({ kind: "not-capturing" });
        else setState({ kind: "ready", gaps: r.gaps });
      } catch (e) {
        if (!live()) return;
        // A deployment without the op, or a role that cannot read it: no tile.
        if (isOpAbsent(e) || (e instanceof ApiError && (e.status === 403 || e.status === 401)))
          setState({ kind: "hidden" });
        else setState({ kind: "error", message: humanizeError(e, { read: true }) });
      }
    },
    [org, namespace],
  );

  if (state.kind === "hidden" || state.kind === "loading") return null;
  return (
    <section aria-label="Knowledge gaps" className="mb-4 rounded-lg border border-border p-3">
      <h2 className="m-0 text-sm font-semibold text-text">Unanswered questions</h2>
      {state.kind === "error" && (
        <p className="m-0 mt-1 text-xs text-text-muted" role="status">
          Couldn't list the unanswered questions — {state.message}
        </p>
      )}
      {state.kind === "not-capturing" && (
        <p className="m-0 mt-1 text-xs text-text-muted" role="status">
          Recording searches is off for this namespace (its settings, or the deployment's default), so no unanswered
          questions are listed.
        </p>
      )}
      {state.kind === "ready" && state.gaps.length === 0 && (
        <p className="m-0 mt-1 text-xs text-text-muted">
          Questions several people asked that no memory or document answered appear here, once a day.
        </p>
      )}
      {state.kind === "ready" && state.gaps.length > 0 && (
        <ul className="m-0 mt-2 list-none space-y-1.5 p-0">
          {state.gaps.map((g) => (
            <li key={g.gap_id} className="text-sm min-w-0">
              <span className="text-text">{g.head}</span>{" "}
              <span className="text-xs text-text-muted tabular">
                {g.askers} askers · {g.asks} times · last asked {g.last_at.slice(0, 10)}
              </span>
              {g.samples.length > 0 && (
                <span className="block truncate text-xs text-text-muted">also: {g.samples.join(" · ")}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
