import { useMemo, useRef, useState, type ReactNode } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { listFacts, searchMemory, type FactRow, type Identity, type SearchHit, type TaxonomyResponse } from "../../api";
import { IconButton } from "../../components/IconButton";
import { Chip, Loading, MetaList, MetaRow, Panel, SectionHeading, TextClamp, Tip } from "../../components/ui";
import { useAbortableEffect } from "../../lib/hooks";
import { colorFor, HOP_COLORS, HOP_EMPHASIS } from "./Canvas";
import {
  asOfInstant,
  relationLabel,
  shorten,
  type Provenance,
  type Selection,
  type WalkModel,
  type XNode,
} from "./model";
import { DirectionIcon } from "./Direction";
import type { ExpandNotice } from "./notices";

/**
 * Inspector — the right pane (300 px, its own scroll). Empty: the legend,
 * short. A node: entity, community, degree (the taxonomy's mention count
 * when it has one, else the edge weight that reached it), the typed facts
 * about it (`facts.list` with `entity=`, sources, provenance tier, the
 * stale mark), the memories naming it with the seed (one clamped line each: excerpt,
 * type, namespace chip when foreign), "why it is here" (the chain from the seed), and the
 * actions: walk from here, path to here, open in Memories, copy, pin, expand, collapse.
 * An edge: relation, direction, co-mentions, validity, provenance, and the rows behind it
 * (`facts.list` with `subject` + `object`, both directions), each with its stale flag.
 *
 * Reads run 250 ms after the selection (so the first click of a
 * double-click never reads) and are aborted when the selection moves on.
 *
 * The pane is a `Panel`; metadata is a `MetaList` whose values wrap ("co-mentioned 381×
 * with logical_error" is read whole); each memory row is a `TextClamp` of one line and
 * 200 characters whose full excerpt is the row button's tooltip; titles wrap, nothing
 * here ends in a silent ellipsis.
 *
 * The pane carries one polite live region, first in the body in every state so it is
 * never remounted, that reads the last expand's result ("Expanded: 0 new within one hop ·
 * 3 already shown") under the node it is about — prefixed with the node's name when
 * another node, or none, is selected.
 */

export interface InspectorProps {
  identity: Identity;
  model: WalkModel | null;
  selection: Selection;
  taxonomy: TaxonomyResponse | null;
  hasCommunities: boolean;
  communityLabel: (cid: number | null) => string;
  asOf: string;
  typed: boolean;
  hops: number;
  provenance: Set<Provenance>;
  memoryTypes: Set<string>;
  tags: Set<string>;
  pinned: Set<string>;
  collapsed: Set<string>;
  hasChildren: (entity: string) => boolean;
  expanding: string | null;
  /** The last expand's result; null when none since the walk. */
  notice?: ExpandNotice | null;
  onWalk: (entity: string) => void;
  onPath: (entity: string) => void;
  onSelect: (sel: Selection) => void;
  onExpand: (entity: string) => void;
  onPin: (entity: string) => void;
  onCollapse: (entity: string, collapsed: boolean) => void;
  onOpenMemory: (key: string) => void;
}

/** The live region for an expand's result. Always rendered, so a screen
 *  reader has the region before its text changes; empty when there is
 *  nothing to say. `about` is the entity the pane is showing. */
function NoticeLine({ notice, about }: { notice: ExpandNotice | null | undefined; about: string | null }) {
  const other = notice && notice.entity !== about;
  return (
    <p
      key="notice"
      role="status"
      aria-live="polite"
      data-testid="inspector-notice"
      className={`text-xs break-words ${notice ? (notice.tone === "error" ? "text-danger" : "text-text") : ""} ${notice ? "mb-2" : "m-0 h-0 overflow-hidden"}`}
    >
      {notice && (
        <>
          {other && (
            <>
              <span className="font-mono">{shorten(notice.entity, 24)}</span>
              {" — "}
            </>
          )}
          {notice.line}
        </>
      )}
    </p>
  );
}

function seedNode(m: WalkModel): XNode {
  return {
    entity: m.seed,
    hop: 0,
    via_co_occur: 0,
    via_relation: "",
    via_direction: "",
    via_provenance: "",
    valid_from: "",
    valid_to: "",
    community_id: m.seedCommunityId,
    path: [m.seed],
    parent: null,
    origin: "walk",
  };
}

/** Rows a list shows before "Show all N" (SPEC §7.2). */
const CAP = 20;

/** The rows to draw: the first 20 of a longer list until "Show all N" is pressed. */
function useCapped<T>(items: T[] | null): { shown: T[]; more: ReactNode } {
  const [all, setAll] = useState(false);
  const list = items ?? [];
  if (all || list.length <= CAP) return { shown: list, more: null };
  return {
    shown: list.slice(0, CAP),
    more: (
      <Button type="empty" htmlType="button" onClick={() => setAll(true)} className="mt-1">
        Show all {list.length}
      </Button>
    ),
  };
}

/** The memory types `memory.search` filters on server-side; any other facet value is filtered client-side only. */
function isSearchMemoryType(t: string): t is "semantic" | "procedural" | "preference" {
  return t === "semantic" || t === "procedural" || t === "preference";
}

function FactList({
  facts,
  about,
  onWalk,
  onOpenMemory,
  emptyText,
}: {
  facts: FactRow[] | null;
  about: string | null;
  onWalk: (e: string) => void;
  onOpenMemory: (k: string) => void;
  emptyText: string;
}) {
  const { shown, more } = useCapped(facts);
  if (facts === null) return <Loading layout="inline" label="Loading facts" />;
  if (facts.length === 0) return <div className="text-xs text-text-muted">{emptyText}</div>;
  return (
    <>
      <ul className="space-y-1">
        {shown.map((f) => {
          const rel = relationLabel(f.relation);
          const link = (e: string) => (
            <Link
              component="button"
              type="button"
              size="sm"
              weight="mono"
              className="break-words text-left"
              onClick={() => onWalk(e)}
            >
              {e}
            </Link>
          );
          return (
            <li
              key={`${f.subject}|${f.relation}|${f.object}`}
              className="text-xs rounded-md border border-border bg-surface px-2 py-1.5"
            >
              <div className="flex items-start gap-1.5">
                <span className="flex-1 min-w-0 leading-snug break-words">
                  {about && f.subject === about ? (
                    <>
                      <span className="text-text-muted">{rel}</span> {link(f.object)}
                    </>
                  ) : about && f.object === about ? (
                    <>
                      {link(f.subject)} <span className="text-text-muted">{rel}</span>
                    </>
                  ) : (
                    <>
                      {link(f.subject)} <span className="text-text-muted">{rel}</span> {link(f.object)}
                    </>
                  )}
                </span>
                <span className="tabular-nums text-text-muted shrink-0">{f.co_occur}×</span>
              </div>
              <div className="mt-0.5 flex items-center gap-1.5 text-xs text-text-muted min-w-0 flex-wrap">
                {f.provenance && <Badge text={f.provenance} state="neutral" size="sm" />}
                <span className="tabular-nums">{f.last_seen ? f.last_seen.slice(0, 10) : ""}</span>
                {f.stale ? (
                  <span className="inline-flex items-center gap-1 text-warn">
                    <Icon name="warning" size="xs" color="var(--color-warn)" aria-hidden="true" /> stale —{" "}
                    {f.sources.length} source row
                    {f.sources.length === 1 ? "" : "s"}, none active
                  </span>
                ) : f.sources.length > 0 ? (
                  <span className="flex items-center gap-1 min-w-0">
                    {f.sources.slice(0, 2).map((sref) => (
                      <Tip key={sref.memory_key} content={`${sref.memory_key} · ${sref.status || "status unknown"}`}>
                        <button
                          type="button"
                          onClick={() => onOpenMemory(sref.memory_key)}
                          aria-label={`Open ${sref.memory_key} (${sref.status || "status unknown"})`}
                          className={`font-mono px-1 rounded border border-border truncate max-w-[110px] hover:text-text ${sref.status === "active" || sref.status === "conflicted" ? "" : "line-through"}`}
                        >
                          {sref.memory_key}
                        </button>
                      </Tip>
                    ))}
                    {f.sources.length > 2 && <span>+{f.sources.length - 2}</span>}
                  </span>
                ) : null}
              </div>
            </li>
          );
        })}
      </ul>
      {more}
    </>
  );
}

export function Inspector({
  identity,
  model,
  selection,
  taxonomy,
  hasCommunities,
  communityLabel,
  asOf,
  typed,
  hops,
  provenance,
  memoryTypes,
  tags,
  pinned,
  collapsed,
  hasChildren,
  expanding,
  notice = null,
  onWalk,
  onPath,
  onSelect,
  onExpand,
  onPin,
  onCollapse,
  onOpenMemory,
}: InspectorProps) {
  const [facts, setFacts] = useState<FactRow[] | null>(null);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [copied, setCopied] = useState(false);
  const [showWhy, setShowWhy] = useState(true);
  const keyRef = useRef<string>("");

  const node: XNode | null = useMemo(() => {
    if (!model || !selection || selection.kind !== "node") return null;
    return selection.entity === model.seed ? seedNode(model) : (model.nodes.get(selection.entity) ?? null);
  }, [model, selection]);
  const edge: { parent: string; child: XNode } | null = useMemo(() => {
    if (!model || !selection || selection.kind !== "edge") return null;
    const c = model.nodes.get(selection.child);
    return c ? { parent: selection.parent, child: c } : null;
  }, [model, selection]);

  const provKey = [...provenance].sort().join(",");
  const typesKey = [...memoryTypes].sort().join(",");
  const tagsKey = [...tags].sort().join(",");

  // The panel's reads: a beat after the selection, aborted when it moves on.
  useAbortableEffect(
    (signal, live) => {
      const sel = node ? `n:${node.entity}` : edge ? `e:${edge.parent}>${edge.child.entity}` : null;
      if (!model || sel === null) {
        setFacts(null);
        setHits(null);
        keyRef.current = "";
        return;
      }
      const key = `${model.seed}|${sel}|${asOf}|${provKey}|${typesKey}|${tagsKey}`;
      if (keyRef.current === key) return;
      setFacts(null);
      setHits(null);
      const id = { org: identity.org, namespace: identity.namespace };
      const prov = provenance.size ? [...provenance] : undefined;
      const timer = window.setTimeout(() => {
        keyRef.current = key;
        if (node) {
          listFacts(id, { entity: node.entity, limit: 12, as_of: asOfInstant(asOf), provenance: prov, signal })
            .then((r) => {
              if (live()) setFacts(r.rows || []);
            })
            .catch(() => {
              if (live()) setFacts([]);
            });
          const both = node.entity === model.seed ? [model.seed] : [model.seed, node.entity];
          // entity_filter is an OR boost, so the list is "memories naming
          // these entities"; the type and tag facets narrow it server-side.
          const only = memoryTypes.size === 1 ? [...memoryTypes][0] : null;
          searchMemory(identity, both.join(" "), {
            top_k: 8,
            entity_filter: both,
            bodies: "excerpt",
            signal,
            ...(tags.size ? { tags: [...tags] } : {}),
            ...(only !== null && isSearchMemoryType(only) ? { memory_type: only } : {}),
          })
            .then((r) => {
              if (live())
                setHits((r.results || []).filter((h) => memoryTypes.size === 0 || memoryTypes.has(h.memory_type)));
            })
            .catch(() => {
              if (live()) setHits([]);
            });
        } else if (edge) {
          const a = edge.parent;
          const b = edge.child.entity;
          Promise.all([
            listFacts(id, { subject: a, object: b, limit: 12, as_of: asOfInstant(asOf), provenance: prov, signal }),
            listFacts(id, { subject: b, object: a, limit: 12, as_of: asOfInstant(asOf), provenance: prov, signal }),
          ])
            .then(([ab, ba]) => {
              if (live()) setFacts([...(ab.rows || []), ...(ba.rows || [])]);
            })
            .catch(() => {
              if (live()) setFacts([]);
            });
        }
      }, 250);
      signal.addEventListener("abort", () => window.clearTimeout(timer));
    },
    [model, node, edge, asOf, provKey, typesKey, tagsKey, identity.org, identity.namespace],
  );

  const degree = (entity: string, fallback: number): { n: number; what: string } => {
    const t = taxonomy?.entities.find((e) => e.value === entity);
    return t
      ? { n: t.count, what: "memories mention it" }
      : { n: fallback, what: "co-mentions on the edge that reached it" };
  };

  const copy = (text: string) => {
    navigator.clipboard
      ?.writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => undefined);
  };

  if (model && edge) {
    const c = edge.child;
    const held =
      c.valid_from || c.valid_to
        ? `${c.valid_from ? c.valid_from.slice(0, 10) : "…"} – ${c.valid_to ? c.valid_to.slice(0, 10) : "now"}`
        : "unbounded";
    const stale = facts ? facts.some((f) => f.stale) : false;
    return (
      <Panel
        as="aside"
        aria-label="Inspector"
        tone="surface-2"
        scroll
        header={
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <SectionHeading level={2} label="Edge" />
              <div className="text-sm font-semibold font-mono break-words">
                {edge.parent} — {c.entity}
              </div>
            </div>
            <IconButton label="Clear selection" onClick={() => onSelect(null)} icon="cross" size="sm" />
          </div>
        }
      >
        <NoticeLine notice={notice} about={null} />
        <MetaList className="mt-2" aria-label="Edge detail">
          <MetaRow label="relation" mono={Boolean(c.via_relation)}>
            {c.via_relation ? (
              relationLabel(c.via_relation)
            ) : (
              <span className="text-text-muted font-sans">co-mention (no typed relation)</span>
            )}
          </MetaRow>
          {c.via_relation && (
            <MetaRow label="direction" mono>
              {c.via_direction === "in" ? `${c.entity} → ${edge.parent}` : `${edge.parent} → ${c.entity}`}
            </MetaRow>
          )}
          <MetaRow label="co-mentions">{c.via_co_occur}×</MetaRow>
          <MetaRow label="held" mono>
            {held}
          </MetaRow>
          <MetaRow label="provenance">
            {c.via_provenance || <span className="text-text-muted">{typed ? "unknown" : "co-occurrence edge"}</span>}
          </MetaRow>
          {stale && (
            <MetaRow label="stale" tone="warn">
              <span className="inline-flex items-center gap-1">
                <Icon name="warning" size="xs" color="var(--color-warn)" aria-hidden="true" /> a row behind this edge
                has no active source
              </span>
            </MetaRow>
          )}
        </MetaList>
        <div className="mt-3 flex items-center gap-0.5 -mx-1">
          <IconButton
            label={`Select ${shorten(c.entity, 24)}`}
            onClick={() => onSelect({ kind: "node", entity: c.entity })}
            icon="chevron-right"
            size="sm"
          />
          <IconButton
            label={`Walk from ${shorten(c.entity, 24)}`}
            onClick={() => onWalk(c.entity)}
            icon="arrow-right"
            size="sm"
          />
          <IconButton
            label="Copy the pair"
            onClick={() => copy(`${edge.parent} ${c.via_relation ? relationLabel(c.via_relation) : "—"} ${c.entity}`)}
            icon="copy"
            size="sm"
          />
          {copied && <span className="text-xs text-text-muted">copied</span>}
        </div>
        <div className="mt-3">
          <SectionHeading label="Rows behind this edge" count={facts ? facts.length : undefined} />
          <FactList
            facts={facts}
            about={null}
            onWalk={onWalk}
            onOpenMemory={onOpenMemory}
            emptyText={
              typed
                ? "No typed triple joins these two; the edge is a co-mention."
                : "No typed triple joins these two in either direction; the edge is a co-mention. Tick “typed edges only” to walk the triples."
            }
          />
        </div>
      </Panel>
    );
  }

  const n = node;
  if (!model || !n) {
    return (
      <Panel
        as="aside"
        aria-label="Inspector"
        tone="surface-2"
        scroll
        className="text-xs text-text-muted"
        header={<SectionHeading level={2} label="Reading the graph" />}
      >
        <NoticeLine notice={notice} about={null} />
        <ul className="space-y-1.5">
          <li className="flex items-center gap-2">
            <span
              className="inline-block w-2.5 h-2.5 rounded-full shrink-0"
              style={{ background: HOP_COLORS[0] }}
              aria-hidden="true"
            />{" "}
            seed; each ring is one hop; a node hangs off the node that reached it
          </li>
          <li>
            {hasCommunities
              ? "colour is the entity's community (left)"
              : "colour is the hop (no communities computed here)"}
            ; size is how often the pair was mentioned together
          </li>
          <li>
            hover lights a chain; click a node or an edge for its detail here; ⊕ expands a node in place; double-click
            walks from it
          </li>
          <li>
            a triple with no active row left is marked <span className="text-warn">stale</span>
          </li>
          <li>the outline under the picture reads the same tree by keyboard</li>
        </ul>
      </Panel>
    );
  }

  const isSeed = n.entity === model.seed;
  const parent = n.parent;
  const deg = degree(n.entity, n.via_co_occur);
  const isPinned = pinned.has(n.entity);
  const isCollapsed = collapsed.has(n.entity);
  const firstHit = hits && hits.length ? hits[0] : null;
  const reachedBy =
    typed && n.via_relation ? (
      <>
        <DirectionIcon direction={n.via_direction} />
        {relationLabel(n.via_relation)} with {n.parent}
      </>
    ) : (
      `co-mentioned ${n.via_co_occur}× with ${n.parent}`
    );
  return (
    <Panel
      as="aside"
      aria-label="Inspector"
      tone="surface-2"
      scroll
      header={
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <SectionHeading level={2} label={isSeed ? "Seed" : "Selected"} />
            <div className="text-sm font-semibold font-mono break-words">{n.entity}</div>
          </div>
          <IconButton label="Clear selection" onClick={() => onSelect(null)} icon="cross" size="sm" />
        </div>
      }
    >
      <NoticeLine notice={notice} about={n.entity} />
      <MetaList className="mt-2" aria-label="Node detail">
        {!isSeed && <MetaRow label="hop">{n.hop}</MetaRow>}
        <MetaRow label="degree">
          {deg.n} — {deg.what}
        </MetaRow>
        {hasCommunities && (
          <MetaRow label="community">
            <span className="inline-flex items-start gap-1.5 min-w-0">
              <span
                className="inline-block w-2.5 h-2.5 mt-1 rounded-full shrink-0"
                style={{ background: colorFor(n, true) }}
                aria-hidden="true"
              />
              <span className="break-words">{communityLabel(n.community_id)}</span>
            </span>
          </MetaRow>
        )}
        {!isSeed && parent !== null && (
          <MetaRow label="reached by">
            <Link
              component="button"
              type="button"
              size="sm"
              onClick={() => onSelect({ kind: "edge", parent, child: n.entity })}
              className="text-left break-words"
            >
              {reachedBy}
            </Link>
          </MetaRow>
        )}
        {(n.valid_from || n.valid_to) && (
          <MetaRow label="held" mono>
            {n.valid_from ? n.valid_from.slice(0, 10) : "…"} – {n.valid_to ? n.valid_to.slice(0, 10) : "now"}
          </MetaRow>
        )}
      </MetaList>

      <div className="mt-3 flex items-center gap-0.5 -mx-1 flex-wrap">
        {!isSeed && <IconButton label="Walk from here" onClick={() => onWalk(n.entity)} icon="arrow-right" size="sm" />}
        {!isSeed && (
          <IconButton
            label={`Path from ${shorten(model.seed, 20)} to here`}
            onClick={() => onPath(n.entity)}
            icon="arrow-directions"
            size="sm"
          />
        )}
        <IconButton
          label={isCollapsed ? "Show hidden children" : "Expand in place (one hop of neighbours)"}
          disabled={expanding === n.entity}
          onClick={() => (isCollapsed ? onCollapse(n.entity, false) : onExpand(n.entity))}
          icon="plus"
          size="sm"
        />
        {hasChildren(n.entity) && !isCollapsed && (
          <IconButton
            label="Collapse children"
            onClick={() => onCollapse(n.entity, true)}
            icon="chevron-down"
            size="sm"
          />
        )}
        {!isSeed && (
          <IconButton
            label={isPinned ? "Unpin" : "Pin (survives facet changes)"}
            onClick={() => onPin(n.entity)}
            icon={isPinned ? "unpin" : "pin"}
            size="sm"
          />
        )}
        <IconButton
          label={
            firstHit ? `Open in Memories: ${firstHit.memory_key}` : "Open in Memories (no memory row names it yet)"
          }
          disabled={!firstHit}
          onClick={() => firstHit && onOpenMemory(firstHit.memory_key)}
          icon="book"
          size="sm"
        />
        <IconButton label="Copy entity" onClick={() => copy(n.entity)} icon="copy" size="sm" />
        {copied && <span className="text-xs text-text-muted">copied</span>}
        {expanding === n.entity && <Loading layout="inline" label="Loading neighbours" />}
      </div>

      {!isSeed && (
        <div className="mt-3">
          <button
            type="button"
            onClick={() => setShowWhy((v) => !v)}
            aria-expanded={showWhy}
            className="flex items-center gap-1 section-label text-text-muted mb-1 hover:text-text"
          >
            <Icon name={showWhy ? "chevron-down" : "chevron-right"} size="xs" aria-hidden="true" /> Why it is here
          </button>
          {showWhy && (
            <ol className="text-xs font-mono space-y-0.5">
              {n.path.map((e, i, arr) => {
                const step = i > 0 ? (e === n.entity ? n : model.nodes.get(e)) : null;
                return (
                  <li key={`${e}-${i}`} className="flex items-start gap-1.5 min-w-0">
                    <span
                      className="inline-block w-2 h-2 mt-1.5 rounded-full shrink-0"
                      style={{
                        background: HOP_COLORS[Math.min(i, HOP_COLORS.length - 1)],
                        opacity: hasCommunities ? 1 : HOP_EMPHASIS[Math.min(i, HOP_EMPHASIS.length - 1)],
                      }}
                      aria-hidden="true"
                    />
                    {/* The last hop is the node shown here, so it is text, not a link. */}
                    {i === arr.length - 1 ? (
                      <span className="min-w-0 break-words">{e}</span>
                    ) : (
                      <Link
                        component="button"
                        type="button"
                        size="sm"
                        weight="mono"
                        onClick={() => onSelect({ kind: "node", entity: e })}
                        className="min-w-0 break-words text-left"
                      >
                        {e}
                      </Link>
                    )}
                    {step && (
                      <span className="text-xs text-text-muted shrink-0 font-sans">
                        {typed && step.via_relation ? relationLabel(step.via_relation) : `${step.via_co_occur}×`}
                      </span>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}

      <div className="mt-4">
        <SectionHeading
          label="Typed facts"
          count={facts ? facts.length : undefined}
          caption={provenance.size ? [...provenance].join(", ") : undefined}
          hint={`Typed triples with ${n.entity} as subject or object (facts.list), with provenance tier, last seen and the source rows behind each.`}
        />
        <FactList
          facts={facts}
          about={n.entity}
          onWalk={onWalk}
          onOpenMemory={onOpenMemory}
          emptyText={
            provenance.size
              ? "No typed triple of the chosen provenance names this entity."
              : "No typed triple names this entity; the walk is co-mention only."
          }
        />
      </div>

      <div className="mt-4">
        <SectionHeading
          label="Memories"
          count={hits ? hits.length : undefined}
          caption={memoryTypes.size || tags.size ? "filtered" : undefined}
          hint={
            isSeed
              ? "Memories naming the seed (memory.search with an entity boost); click one to open it."
              : "Memories naming the seed or this entity (memory.search with an entity boost — an OR, so a row may name only one of them); click one to open it."
          }
        />
        {hits === null && <Loading layout="inline" label="Loading memories" />}
        {hits && hits.length === 0 && (
          <div className="text-xs text-text-muted">
            {memoryTypes.size || tags.size
              ? "No memory row of the chosen type or tags names either entity."
              : "No memory row names either entity; the edge may come from a chunk or a document."}
          </div>
        )}
        <ul className="space-y-1">
          {(hits || []).map((h) => {
            const excerpt = h.content_excerpt || h.memory_key;
            return (
              <li key={h.memory_key}>
                <Tip
                  content={
                    excerpt.length > 200 ? `${excerpt.slice(0, 600)}${excerpt.length > 600 ? "…" : ""}` : excerpt
                  }
                  side="left"
                >
                  <button
                    type="button"
                    onClick={() => onOpenMemory(h.memory_key)}
                    className="w-full text-left text-xs rounded-md border border-border bg-surface px-2 py-1.5 hover:border-border-strong"
                  >
                    <TextClamp text={excerpt} lines={1} max={200} reach="parent" className="leading-snug" />
                    <div className="mt-0.5 flex items-center gap-1.5 min-w-0 text-xs text-text-muted">
                      <Badge text={h.memory_type} state="neutral" size="sm" className="shrink-0" />
                      {h.created_at && <span className="tabular-nums shrink-0">{h.created_at.slice(0, 10)}</span>}
                      <span className="font-mono truncate flex-1">{h.memory_key}</span>
                      {h.namespace && h.namespace !== identity.namespace && (
                        <Chip label={h.namespace} mono className="shrink-0 min-h-5" />
                      )}
                    </div>
                  </button>
                </Tip>
              </li>
            );
          })}
        </ul>
      </div>
      <p className="mt-3 caption">
        within {hops} hop{hops === 1 ? "" : "s"} of {shorten(model.seed, 20)}
        {asOf ? ` · as of ${asOf}` : ""}
      </p>
    </Panel>
  );
}
