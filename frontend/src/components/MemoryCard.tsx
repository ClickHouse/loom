import { memo, useId, useMemo, useState, type ReactNode } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Icon } from "@clickhouse/click-ui/Icon";
import type { Identity, MemoryListEntry } from "../api";
import { getMemory } from "../api";
import { AgentBadge } from "./AgentBadge";
import { ListRow, TagList } from "./ui";
import { ListTime } from "./ui/ListTime";
import { truncate, previewValue } from "../lib/format";
import { detectEncodedValue, detectEncodedRecord } from "../lib/encoded";
import { useAbortableEffect } from "../lib/hooks";

/** The bounded preview a card shows for a value; whether it was an agent-encoded (base64)
 *  envelope whose decoded text is shown instead; and whether it is a JSON object, shown as
 *  its `k: v` pairs. */
function describe(v: unknown): { value: string; encoded: boolean; pairs: boolean } {
  const enc = typeof v === "string" ? detectEncodedValue(v) : detectEncodedRecord(v);
  if (enc) return { value: enc.decoded, encoded: true, pairs: false };
  const pairs = valuePairs(v);
  return pairs
    ? { value: pairs, encoded: false, pairs: true }
    : { value: previewValue(v), encoded: false, pairs: false };
}

/** A JSON object value (an object, or a string holding one) as one line of its pairs:
 *  "steps: drain, re-point, replay · owner: oncall · sla_minutes: 30". A list of scalars
 *  reads as its items; a nested object stays compact JSON. Null for anything else, a cut
 *  preview included (it does not parse). */
export function valuePairs(v: unknown): string | null {
  let obj = v;
  if (typeof v === "string") {
    if (!v.trimStart().startsWith("{")) return null;
    try {
      obj = JSON.parse(v);
    } catch {
      return null;
    }
  }
  if (obj === null || typeof obj !== "object" || Array.isArray(obj)) return null;
  const entries = Object.entries(obj);
  if (entries.length === 0) return null;
  const scalar = (x: unknown) => (x !== null && typeof x === "object" ? JSON.stringify(x) : String(x));
  return entries
    .map(
      ([k, x]) =>
        `${k}: ${Array.isArray(x) && x.every((i) => i === null || typeof i !== "object") ? x.join(", ") : scalar(x)}`,
    )
    .join(" · ");
}

interface MemoryCardProps {
  identity: Identity;
  entry: MemoryListEntry;
  onSelect: (entry: MemoryListEntry) => void;
  /** Space on the card: the Memories list selects it instead of opening it. */
  onToggle?: (key: string) => void;
  active: boolean;
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
  /** A control at the end of the row's first line, after the meta (a search hit's "Why this
   *  result"): beside the open button, never inside it. */
  aside?: ReactNode;
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
  hasConflict = false,
  onTagClick,
  activeTags,
  embedded = false,
  value: preloaded,
  aside,
}: MemoryCardProps) {
  // A card with a preloaded value computes its preview during render — one render per
  // card, not a mount with "…" followed by an effect, two state writes and a second
  // render for every card on the page (a page of 99 cards paid ~220 ms of script for that
  // at 4x CPU). The base64 envelope check runs on the parsed object, or on a string value
  // directly; the inspector still shows both forms in full.
  const preview = useMemo(() => (preloaded === undefined ? null : describe(preloaded)), [preloaded]);
  // For a parent that has no value: one memory.get per card, shown when it lands.
  const [fetched, setFetched] = useState<ReturnType<typeof describe> | null>(null);

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
  const pairs = shown ? shown.pairs : false;
  const id = useId();

  // The row's body is one button stretched over the whole row (its ::after); the tag chips
  // sit beside it, above the stretch, so no control is nested in another.
  const body = (
    <>
      <div className="flex items-start gap-2">
        <button
          type="button"
          data-memory-open=""
          data-row-open=""
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
          // Named by its key; its state, writer, time and value are its description.
          aria-label={entry.memory_key}
          aria-describedby={`${id}-meta ${id}-value`}
          className="list-row-open block min-w-0 flex-1"
        >
          {/* Line 1: what it is, its state, who wrote it and when. */}
          <span id={`${id}-meta`} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
            {/* The name already: left out of the description. */}
            <span aria-hidden="true" className="min-w-0 truncate font-mono text-text">
              {entry.memory_key}
            </span>
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
            {/* The meta slot: who wrote it, then when. Flush right; under 640 px its own line. */}
            <span className="flex min-w-0 basis-full items-center gap-2 text-text-muted sm:ml-auto sm:basis-auto sm:shrink-0">
              {entry.agent && <AgentBadge agent={entry.agent} quiet />}
              <ListTime iso={entry.created_at} quiet />
            </span>
          </span>
          {/* Line 2: the value, two lines at most; a JSON object as one line of its pairs. */}
          {pairs && value !== null ? (
            <span id={`${id}-value`} className="mt-1 block truncate font-mono text-xs text-text-muted">
              {truncate(value, 280)}
            </span>
          ) : (
            <span id={`${id}-value`} className="mt-1 text-sm text-text leading-normal line-clamp-2 break-words">
              {/* Cut before the clamp, so a 2 KB preview never lands in the DOM: two lines hold ~280. */}
              {value === null ? <span className="text-text-muted">…</span> : truncate(value, 280)}
            </span>
          )}
        </button>
        {aside && <div className="relative z-[1] -my-1 flex shrink-0 items-center">{aside}</div>}
      </div>
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
      // Not positioned: the open button's stretch covers the whole row it sits in.
      <div data-memory-key={entry.memory_key} className="group">
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
