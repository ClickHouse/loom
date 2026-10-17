import { memo, useMemo, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Icon } from "@clickhouse/click-ui/Icon";
import type { Identity, MemoryListEntry } from "../api";
import { getMemory } from "../api";
import { AgentBadge } from "./AgentBadge";
import { ListRow, TagList } from "./ui";
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
  onSelect: (entry: MemoryListEntry) => void;
  /** Space on the card: the Memories list selects it instead of opening it. */
  onToggle?: (key: string) => void;
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
  /** Tags the list is filtered by, lowercased: their chips read as pressed. */
  activeTags?: ReadonlySet<string>;
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

// Memoised: a page lists ~100 cards, and each Click UI Badge and Icon re-measures its text
// on every render, so closing the inspector re-rendered every card (~110 ms at 4x CPU). With
// stable callbacks from the parent only the cards whose props changed render again.
export const MemoryCard = memo(function MemoryCard({
  identity,
  entry,
  onSelect,
  onToggle,
  active,
  timeMode = "relative",
  hasConflict = false,
  onTagClick,
  activeTags,
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

  // The row's body is one button stretched over the whole row (its ::after); the tag chips
  // sit beside it, above the stretch, so no control is nested in another.
  const body = (
    <>
      <button
        type="button"
        data-memory-open=""
        onClick={() => onSelect(entry)}
        // A button clicks on Space's keyup, so both halves are taken.
        onKeyDown={
          onToggle &&
          ((e) => {
            if (e.key !== " ") return;
            e.preventDefault();
            onToggle(entry.memory_key);
          })
        }
        onKeyUp={onToggle && ((e) => e.key === " " && e.preventDefault())}
        className="block w-full text-left bg-transparent border-0 p-0 cursor-pointer after:absolute after:inset-0 after:content-['']"
      >
        {/* Line 1: what it is, its state, who wrote it and when. */}
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          <span className="min-w-0 truncate font-mono text-text">{entry.memory_key}</span>
          {entry.pinned && (
            <>
              <Icon name="star" size="xs" color="var(--global-color-text-muted)" aria-hidden="true" />
              <span className="sr-only">, pinned</span>
            </>
          )}
          {(entry.verification === "human" || entry.verification === "process") && (
            <>
              <Icon
                name="secure"
                size="xs"
                color="var(--global-color-text-muted)"
                aria-hidden="true"
                className="shrink-0"
              />
              <span className="sr-only">, verified by {entry.verified_by || "—"}</span>
            </>
          )}
          {hasConflict && (
            <span className="inline-flex shrink-0">
              <Badge text="Conflicted" state="warning" size="sm" aria-hidden="true" />
              <span className="sr-only">, conflicts with another memory in the namespace — open to review</span>
            </span>
          )}
          {entry.verification === "stale" && (
            <span className="inline-flex shrink-0">
              <Badge text="Stale verification" state="warning" size="sm" aria-hidden="true" />
              <span className="sr-only">
                , verified by {entry.verified_by || "—"}, then edited — verification is stale
              </span>
            </span>
          )}
          {entry.tombstoned_at && (
            <span className="inline-flex shrink-0">
              <Badge text="Deleted" state="danger" size="sm" aria-hidden="true" />
              <span className="sr-only">, deleted</span>
            </span>
          )}
          {encoded && (
            <span className="inline-flex shrink-0">
              <Badge text="b64" state="neutral" size="sm" aria-hidden="true" />
              <span className="sr-only">, agent-encoded (base64) value — the preview shows the decoded text</span>
            </span>
          )}
          <span className="ml-auto flex shrink-0 items-center gap-2 text-text-muted">
            {entry.agent && <AgentBadge agent={entry.agent} />}
            <span className="tabular">
              {timeMode === "absolute" ? timeOfDay(entry.created_at) : relativeTime(entry.created_at)}
            </span>
            <Icon name="chevron-right" size="sm" aria-hidden="true" />
          </span>
        </span>
        {/* Line 2: the value, two lines at most. */}
        <span className="mt-1 text-sm text-text leading-normal line-clamp-2 break-words">
          {/* Cut before the clamp, so a 2 KB preview never lands in the DOM: two lines hold ~280. */}
          {value === null ? <span className="text-text-muted">…</span> : truncate(value, 280)}
        </span>
      </button>
      <TagList
        tags={entry.tags ?? []}
        onTagClick={onTagClick}
        activeTags={activeTags}
        className="mt-2 pointer-events-none [&>*]:pointer-events-auto"
      />
    </>
  );

  if (embedded) {
    return (
      <div data-memory-key={entry.memory_key} className="group relative">
        {body}
      </div>
    );
  }
  return (
    <ListRow data-memory-key={entry.memory_key} data-card="" active={active} className="group">
      {body}
    </ListRow>
  );
});
