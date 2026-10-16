import { useMemo, useState } from "react";
import { Star, ChevronRight, AlertTriangle, ShieldCheck, ShieldAlert } from "lucide-react";
import type { Identity, MemoryListEntry } from "../api";
import { getMemory } from "../api";
import { AgentBadge } from "./AgentBadge";
import { relativeTime, timeOfDay, truncate, previewValue } from "../lib/format";
import { detectEncodedValue, detectEncodedRecord } from "../lib/encoded";
import { useAbortableEffect } from "../lib/hooks";

/** The bounded preview a card shows for a value, and whether the value was
 *  an agent-encoded (base64) envelope whose decoded text is shown instead. */
function describe(v: unknown): { value: string; encoded: boolean } {
  const enc = typeof v === "string" ? detectEncodedValue(v) : detectEncodedRecord(v);
  return enc ? { value: enc.decoded, encoded: true } : { value: previewValue(v), encoded: false };
}

interface MemoryCardProps {
  identity: Identity;
  entry: MemoryListEntry;
  onSelect: (key: string) => void;
  active: boolean;
  /** Show full ISO time prefix vs relative time. */
  timeMode?: "absolute" | "relative";
  /** When true, the card flags an active conflict on this memory_key.
   *  Parent (Recent/Pinned) fetches conflicts.list once per namespace
   *  and passes the result down so each card doesn't refetch. */
  hasConflict?: boolean;
  /** Clicking a tag chip on the card calls this with the tag.
   *  Parent decides whether to filter, search, or navigate. */
  onTagClick?: (tag: string) => void;
  /** When set, the card omits its own border + background + padding so
   *  a parent wrapper can host the visual card (e.g. the Recent view's
   *  bulk-select row, where the checkbox lives inside the same card
   *  border as the content). The button still owns click + keyboard. */
  embedded?: boolean;
  /** The row's value, when the parent already has it — memory.list carries
   *  a preview and Recent fills gaps with one memory.bulk_get. `undefined` means "not
   *  known": the card then falls back to its own memory.get. */
  value?: unknown;
}

export function MemoryCard({
  identity,
  entry,
  onSelect,
  active,
  timeMode = "relative",
  hasConflict = false,
  onTagClick,
  embedded = false,
  value: preloaded,
}: MemoryCardProps) {
  // A card with a preloaded value computes its preview during render — one render per
  // card, not a mount with "…" followed by an effect, two state writes and a second
  // render for every card on the page (a page of 99 cards paid ~220 ms of script for that
  // at 4x CPU). The base64 envelope check runs on the parsed object, or on a string value
  // directly; the inspector still shows both forms in full.
  const preview = useMemo(() => (preloaded === undefined ? null : describe(preloaded)), [preloaded]);
  // Fallback for a parent that has no value (older list shapes): one
  // memory.get per card, shown when it lands.
  const [fetched, setFetched] = useState<{ value: string; encoded: boolean } | null>(null);

  useAbortableEffect(
    (signal, live) => {
      if (preloaded !== undefined) return;
      // memory.get requires the strict identity tuple that wrote the row. The
      // list view queries cross-agent (agent="*"), so we use each entry's own
      // agent from the list response, falling back to the current
      // identity.agent only when the server didn't populate it.
      const writerIdentity: Identity = {
        ...identity,
        agent: entry.agent || identity.agent,
      };
      getMemory(writerIdentity, entry.memory_key, { signal })
        .then((res) => {
          if (!live()) return;
          if (res && res.found) setFetched(describe(res.value));
        })
        .catch(() => {
          /* fail-quiet — card just shows no value */
        });
    },
    [identity.org, identity.namespace, identity.agent, entry.memory_key, entry.agent, preloaded],
  );

  const shown = preview ?? fetched;
  const value = shown ? shown.value : null;
  const encoded = shown ? shown.encoded : false;

  return (
    <button
      type="button"
      data-memory-key={entry.memory_key}
      onClick={() => onSelect(entry.memory_key)}
      className={
        embedded
          ? "w-full text-left transition group bg-transparent border-0 p-0 cursor-pointer"
          : `w-full text-left p-3.5 rounded-xl border transition group [content-visibility:auto] [contain-intrinsic-size:auto_112px] ${
              active
                ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]"
                : "border-[var(--color-border)] bg-[var(--color-surface)] hover:border-[var(--color-border-strong)] hover:bg-[var(--color-surface-2)]"
            }`
      }
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="font-mono text-xs text-[var(--color-text-muted)] truncate">{entry.memory_key}</span>
            {entry.pinned && <Star size={12} className="text-warn fill-warn shrink-0" aria-label="Pinned" />}
            {(entry.verification === "human" || entry.verification === "process") && (
              <span title={`Verified by ${entry.verified_by || "—"}`} className="inline-flex shrink-0">
                <ShieldCheck size={12} className="text-success" aria-label="Verified" />
              </span>
            )}
            {entry.verification === "stale" && (
              <span
                title={`Verified by ${entry.verified_by || "—"}, then edited — verification is stale`}
                className="inline-flex shrink-0"
              >
                <ShieldAlert size={12} className="text-warn" aria-label="Verification stale" />
              </span>
            )}
            {encoded && (
              <span
                title="Agent-encoded (base64) value — preview shows the decoded text"
                className="inline-flex shrink-0 text-xs font-medium text-[var(--color-warn)] border border-[var(--color-warn)] rounded px-1 leading-4"
              >
                b64
              </span>
            )}
            {hasConflict && (
              <span
                title="This memory conflicts with another in the namespace — open to review"
                className="inline-flex shrink-0"
              >
                <AlertTriangle size={12} className="text-[var(--color-warn)]" aria-label="Has conflict" />
              </span>
            )}
          </div>
          <div className="text-base text-[var(--color-text)] leading-snug">
            {value === null ? <span className="text-[var(--color-text-subtle)]">…</span> : truncate(value, 140)}
          </div>
          <div className="mt-2 flex items-center gap-2 flex-wrap">
            {entry.tags &&
              entry.tags.length > 0 &&
              entry.tags.slice(0, 4).map((t) =>
                onTagClick ? (
                  <button
                    type="button"
                    key={t}
                    onClick={(e) => {
                      e.stopPropagation();
                      onTagClick(t);
                    }}
                    className="inline-flex items-center text-2xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)] px-1.5 py-0.5 rounded hover:bg-[var(--color-accent-soft)] hover:text-[var(--color-accent)] transition-colors cursor-pointer"
                    title={`Filter by tag: ${t}`}
                  >
                    <span className="opacity-50 mr-0.5">#</span>
                    {t}
                  </button>
                ) : (
                  <span
                    key={t}
                    className="inline-flex items-center text-2xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)] px-1.5 py-0.5 rounded"
                  >
                    <span className="opacity-50 mr-0.5">#</span>
                    {t}
                  </span>
                ),
              )}
            <span className="text-2xs text-[var(--color-text-subtle)] tabular ml-auto">
              {timeMode === "absolute" ? timeOfDay(entry.created_at) : relativeTime(entry.created_at)}
            </span>
          </div>
        </div>
        <div className="flex flex-col items-end gap-1.5 shrink-0">
          <ChevronRight
            size={14}
            className="text-[var(--color-text-subtle)] group-hover:text-[var(--color-text)] transition-colors"
          />
        </div>
      </div>
      {entry.agent && (
        <div className="mt-2">
          <AgentBadge agent={entry.agent} />
        </div>
      )}
    </button>
  );
}
