import { useMemo, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Icon } from "@clickhouse/click-ui/Icon";
import type { Identity, MemoryListEntry } from "../api";
import { getMemory } from "../api";
import { AgentBadge } from "./AgentBadge";
import { Chip } from "./ui";
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

  const card = embedded
    ? "relative"
    : `relative p-3.5 rounded-xl border transition-colors [content-visibility:auto] [contain-intrinsic-size:auto_112px] ${
        active
          ? "border-accent bg-accent-soft"
          : "border-border bg-surface hover:border-border-strong hover:bg-surface-2"
      }`;

  // The card's body is one button stretched over the whole card (its ::after); the tag
  // chips sit beside it, above the stretch, so no control is nested in another.
  return (
    <div data-memory-key={entry.memory_key} className={`group ${card}`}>
      <button
        type="button"
        onClick={() => onSelect(entry.memory_key)}
        className="flex w-full items-start justify-between gap-3 text-left bg-transparent border-0 p-0 cursor-pointer after:absolute after:inset-0 after:rounded-xl after:content-['']"
      >
        <span className="block min-w-0 flex-1">
          <span className="flex items-center gap-2 mb-1.5">
            <span className="font-mono text-xs text-text-muted truncate">{entry.memory_key}</span>
            {entry.pinned && (
              <>
                <Icon name="star" size="xs" color="var(--color-warn)" aria-hidden="true" />
                <span className="sr-only">, pinned</span>
              </>
            )}
            {(entry.verification === "human" || entry.verification === "process") && (
              <>
                <Icon name="secure" size="xs" color="var(--color-success)" aria-hidden="true" className="shrink-0" />
                <span className="sr-only">, verified by {entry.verified_by || "—"}</span>
              </>
            )}
            {entry.verification === "stale" && (
              <>
                <Icon name="warning" size="xs" color="var(--color-warn)" aria-hidden="true" className="shrink-0" />
                <span className="sr-only">
                  , verified by {entry.verified_by || "—"}, then edited — verification is stale
                </span>
              </>
            )}
            {encoded && (
              <span className="inline-flex shrink-0">
                <Badge text="b64" state="warning" size="sm" aria-hidden="true" />
                <span className="sr-only">, agent-encoded (base64) value — the preview shows the decoded text</span>
              </span>
            )}
            {hasConflict && (
              <>
                <Icon name="warning" size="xs" color="var(--color-warn)" aria-hidden="true" className="shrink-0" />
                <span className="sr-only">, conflicts with another memory in the namespace — open to review</span>
              </>
            )}
          </span>
          <span className="block text-sm text-text leading-snug">
            {value === null ? <span className="text-text-subtle">…</span> : truncate(value, 140)}
          </span>
        </span>
        <Icon name="chevron-right" size="sm" aria-hidden="true" className="shrink-0" />
      </button>
      <div className="relative mt-2 flex items-center gap-2 flex-wrap pointer-events-none">
        {(entry.tags ?? []).slice(0, 4).map((t) => (
          <Chip
            key={t}
            label={t}
            icon="#"
            onClick={onTagClick ? () => onTagClick(t) : undefined}
            ariaLabel={onTagClick ? `Filter by tag: ${t}` : undefined}
            className="pointer-events-auto"
          />
        ))}
        <span className="text-2xs text-text-subtle tabular ml-auto">
          {timeMode === "absolute" ? timeOfDay(entry.created_at) : relativeTime(entry.created_at)}
        </span>
      </div>
      {entry.agent && (
        <div className="relative mt-2 pointer-events-none">
          <AgentBadge agent={entry.agent} />
        </div>
      )}
    </div>
  );
}
