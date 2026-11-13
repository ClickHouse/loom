import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import type { PathResponse, TaxonomyEntry } from "../../api";
import { EmptyState } from "../../components/EmptyState";
import { Chip, LoadingSkeleton } from "../../components/ui";
import { Skeleton } from "../../components/Skeleton";
import { relationLabel, shorten, type Selection, type VisibleTree, type WalkModel, type XNode } from "./model";
import { DirectionIcon, directionWord } from "./Direction";

/**
 * Canvas — the walk's picture: a fixed `aspect-[820/560]` box, hover as DOM attributes
 * (`data-hover` on the svg, `data-hot` on the lit chain), one always-present readout,
 * memoised nodes and edges, a 12 px hit circle on every node, no React state on hover.
 * adds, without touching that:
 *
 * - **Edges are targets.** Each edge is a `<g class="graph-edge">` holding
 *   a 10 px transparent hit line and the drawn line; hovering one lights the
 *   child's chain and the readout names the pair; clicking selects the edge.
 * - **Expand in place.** Every node group carries a ⊕ group
 *   (`data-action="expand"`) that CSS shows on `:hover` and on the selected
 *   node (`data-selected`) — the same delegated click handler reads the
 *   action, so no per-node handler and no state on hover. A collapsed node
 *   shows a `+N` badge (`data-action="uncollapse"`) instead. A pinned node
 *   wears a dashed ring.
 * - **Path mode** lays the chain out on one line with the edge annotated
 *   (relation and direction or co-mentions; validity window) and states
 *   "no path within N hops" as a result in the card, never an empty card.
 */

const W = 820;
const H = 560;
// Svg units per screen pixel on the 566 px card of a 1400 px window, the scale a first
// render starts from before the card is measured.
const USUAL_SCALE = 1.45;

const CATEGORICAL = Array.from({ length: 8 }, (_, i) => `var(--color-cat-${i + 1})`);
export const COMMUNITY_PALETTE = CATEGORICAL;
export const HOP_COLORS = CATEGORICAL.slice(0, 4);
/** Fill opacity by hop when colour is the hop: the seed brightest, the outer rings muted.
 *  Mirrors the `.graph-dot` rules in styles.css; the legend and summary swatches use it
 *  too. */
export const HOP_EMPHASIS = [1, 0.8, 0.45, 0.3];

export type LabelMode = "key" | "all" | "none";

export function colorFor(n: { community_id: number | null | undefined; hop: number }, hasCommunities: boolean): string {
  if (hasCommunities && n.community_id !== null && n.community_id !== undefined) {
    return COMMUNITY_PALETTE[Math.abs(n.community_id) % COMMUNITY_PALETTE.length];
  }
  return HOP_COLORS[Math.min(n.hop, HOP_COLORS.length - 1)];
}

export interface Placed extends XNode {
  /** DOM id (`data-id`) for the hover lighting; "s" is the seed. */
  id: string;
  x: number;
  y: number;
  r: number;
  angle: number;
  /** Whether this node's label is drawn in "key" mode (budgeted per ring). */
  keyLabel: boolean;
  /** Svg units between the node and the card's edge that its label may take. */
  labelRoom: number;
}

function labelRoom(x: number, r: number, angle: number, width: number): number {
  const cos = Math.cos(angle);
  return Math.abs(cos) < 0.25 ? Math.min(x, width - x) * 2 - 8 : (cos > 0 ? width - x : x) - r - 8;
}

/** How many characters of a label fit in `room` svg units. A character is ~0.6 em
 *  of the 12 px label floor (--canvas-label-min), times `scale` svg units per screen
 *  pixel, so a narrow card fits fewer. The rest is an explicit ellipsis, never a clip;
 *  the full name is always in the readout, the outline and the inspector. */
function labelFit(room: number, scale: number): number {
  return Math.max(4, Math.min(24, Math.floor(room / (12 * 0.6 * scale))));
}

interface Layout {
  seed: Placed;
  nodes: Placed[];
  rings: number[];
  linear: boolean;
}

function seedPlaced(m: WalkModel, x: number, y: number, maxW: number): Placed {
  return {
    entity: m.seed,
    hop: 0,
    via_co_occur: maxW,
    via_relation: "",
    via_direction: "",
    via_provenance: "",
    valid_from: "",
    valid_to: "",
    community_id: m.seedCommunityId,
    path: [m.seed],
    parent: null,
    origin: "walk",
    id: "s",
    x,
    y,
    r: 11,
    angle: 0,
    keyLabel: true,
    labelRoom: 2 * x - 8,
  };
}

/** Radial layout by hop. Ring radius grows with the ring's population so nodes keep a
 *  minimum arc between them; a ring is ordered by the parent's angle so a subtree hangs
 *  together; the label budget per ring is the number of ~64 px slots on its
 *  circumference, given to the strongest nodes. */
function radialLayout(m: WalkModel, nodes: XNode[], width: number, height: number): Layout {
  const cx = width / 2;
  const cy = height / 2;
  const maxHop = Math.max(1, ...nodes.map((n) => n.hop));
  const byHop = new Map<number, XNode[]>();
  for (const n of nodes) byHop.set(n.hop, [...(byHop.get(n.hop) || []), n]);
  const maxW = Math.max(1, ...nodes.map((n) => n.via_co_occur));
  // Above the top ring node and below the bottom one sits a label: 44 units is its
  // height at the 12 px floor on a ~420 px card (1280 px wide window).
  const maxR = Math.min(cx - 28, cy - 44);
  const radii: number[] = [];
  for (let hop = 1; hop <= maxHop; hop++) {
    const count = (byHop.get(hop) || []).length;
    const even = (maxR * hop) / maxHop;
    const needed = (count * 26) / (2 * Math.PI);
    radii.push(Math.min(maxR, Math.max(even, needed, (radii[hop - 2] ?? 0) + 70)));
  }
  const placed: Placed[] = [];
  const angleOf = new Map<string, number>([[m.seed, -Math.PI / 2]]);
  for (let hop = 1; hop <= maxHop; hop++) {
    const list = byHop.get(hop) || [];
    if (!list.length) continue;
    const radius = radii[hop - 1] ?? maxR;
    const ordered = [...list].sort((a, b) => {
      const pa = angleOf.get(a.parent ?? m.seed) ?? 0;
      const pb = angleOf.get(b.parent ?? m.seed) ?? 0;
      return hop === 1 ? b.via_co_occur - a.via_co_occur : pa - pb || b.via_co_occur - a.via_co_occur;
    });
    const step = (2 * Math.PI) / ordered.length;
    // One label slot is ~80 svg units of arc: the label floor draws canvas text at ≥ 11
    // screen px, i.e. ~15 svg units at a 554 px canvas, so a slot of 62 would let
    // neighbours touch.
    const minGap = Math.min(Math.PI / 6, 80 / radius);
    const byStrength = ordered.map((n, i) => ({ n, i })).sort((a, b) => b.n.via_co_occur - a.n.via_co_occur);
    const labelledAngles: number[] = [];
    const labelled = new Set<string>();
    for (const { n, i } of byStrength) {
      const a = i * step;
      if (labelledAngles.every((b) => Math.abs(((a - b + Math.PI) % (2 * Math.PI)) - Math.PI) >= minGap)) {
        labelledAngles.push(a);
        labelled.add(n.entity);
      }
    }
    ordered.forEach((n, i) => {
      const angle = i * step - Math.PI / 2 + (hop === 1 ? 0 : step / 2);
      angleOf.set(n.entity, angle);
      const x = cx + radius * Math.cos(angle);
      const r = 4 + 8 * Math.sqrt(n.via_co_occur / maxW);
      placed.push({
        ...n,
        id: String(placed.length),
        x,
        y: cy + radius * Math.sin(angle),
        r,
        angle,
        keyLabel: labelled.has(n.entity),
        labelRoom: labelRoom(x, r, angle, width),
      });
    });
  }
  return { seed: seedPlaced(m, cx, cy, maxW), nodes: placed, rings: radii, linear: false };
}

/** Path mode: the chain on one line, left to right, every node labelled. */
function linearLayout(m: WalkModel, nodes: XNode[], width: number, height: number): Layout {
  const ordered = [...nodes].sort((a, b) => a.hop - b.hop);
  const count = ordered.length + 1;
  const left = 90;
  const stepX = count > 1 ? (width - 2 * left) / (count - 1) : 0;
  const y = height / 2;
  const maxW = Math.max(1, ...ordered.map((n) => n.via_co_occur));
  const placed = ordered.map((n, i) => ({
    ...n,
    id: String(i),
    x: left + stepX * (i + 1),
    y,
    r: 6 + 6 * Math.sqrt(n.via_co_occur / maxW),
    angle: Math.PI / 2,
    keyLabel: true,
    labelRoom: stepX || width,
  }));
  return { seed: seedPlaced(m, left, y, maxW), nodes: placed, rings: [], linear: true };
}

/** Label placement: outward from the ring, anchored by which side the node
 *  sits on, so labels never cross the ring inwards over other nodes. A label above
 *  or below its node stays inside the card: its baseline is clamped by the label's
 *  height in svg units (the 12 px floor times `scale`). */
function labelProps(n: Placed, scale: number): { dx: number; dy: number; anchor: "start" | "end" | "middle" } {
  const cos = Math.cos(n.angle);
  const sin = Math.sin(n.angle);
  if (Math.abs(cos) < 0.25) {
    const size = 12 * scale;
    const dy = sin > 0 ? n.r + 12 : -n.r - 5;
    return { dx: 0, dy: Math.min(H - 2 - size * 0.3, Math.max(2 + size, n.y + dy)) - n.y, anchor: "middle" };
  }
  return { dx: cos > 0 ? n.r + 4 : -n.r - 4, dy: 4, anchor: cos > 0 ? "start" : "end" };
}

/** Width of a `+N` badge in svg units, sized for digits at the canvas floor. */
function badgeWidth(n: number): number {
  return String(n).length * 9 + 16;
}

function edgeText(n: XNode, typed: boolean): string {
  return typed && n.via_relation
    ? `${relationLabel(n.via_relation)} (${directionWord(n.via_direction)})`
    : `co-mentioned ${n.via_co_occur}×`;
}

function window_(n: XNode): string {
  if (!n.valid_from && !n.valid_to) return "";
  return `${n.valid_from ? n.valid_from.slice(0, 10) : "…"} – ${n.valid_to ? n.valid_to.slice(0, 10) : "now"}`;
}

// ---- Memoised picture elements: a hover is a DOM attribute, a selection
// ---- changes the props of the two or three elements it touches.

interface EdgeProps {
  id: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  weight: number;
  sel: boolean;
  /** Path mode: the annotation drawn along the edge. */
  note?: string;
  note2?: string;
}

const GraphEdge = memo(function GraphEdge({ id, x1, y1, x2, y2, weight, sel, note, note2 }: EdgeProps) {
  const w = Math.max(0.75, Math.min(3, weight / 4));
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  const style: CSSProperties & { "--w": number } = { "--w": w };
  return (
    <g className="graph-edge" data-id={`e${id}`} data-sel={sel ? "" : undefined} style={style}>
      <line className="graph-edge-hit" x1={x1} y1={y1} x2={x2} y2={y2} />
      <line className="graph-edge-line" x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--color-border-strong)" />
      {note && (
        <text className="graph-edge-note" x={mx} y={my - 12} textAnchor="middle" fill="var(--color-text-muted)">
          {note}
        </text>
      )}
      {note2 && (
        <text className="graph-edge-note" x={mx} y={my + 22} textAnchor="middle" fill="var(--color-text-muted)">
          {note2}
        </text>
      )}
    </g>
  );
});

interface NodeProps {
  n: Placed;
  color: string;
  showLabel: boolean;
  showRelation: boolean;
  selected: boolean;
  sel: boolean;
  pinned: boolean;
  /** Descendants hidden under this node when collapsed; 0 = not collapsed. */
  hidden: number;
  /** Svg units per screen pixel: how long and how tall a label is. */
  scale: number;
}

const GraphNode = memo(function GraphNode({
  n,
  color,
  showLabel,
  showRelation,
  selected,
  sel,
  pinned,
  hidden,
  scale,
}: NodeProps) {
  const lp = labelProps(n, scale);
  const ax = n.r + 7;
  const ay = -n.r - 7;
  return (
    <g
      className="graph-node cursor-pointer"
      data-id={n.id}
      data-sel={sel ? "" : undefined}
      data-selected={selected ? "" : undefined}
      data-hop={Math.min(n.hop, 3)}
      transform={`translate(${n.x},${n.y})`}
    >
      {/* the hit target: never smaller than 12 px, whatever the node's size */}
      <circle r={Math.max(12, n.r + 4)} fill="transparent" />
      {pinned && (
        <circle r={n.r + 4} fill="none" stroke="var(--color-accent)" strokeWidth={1.5} strokeDasharray="3 2" />
      )}
      <circle
        className="graph-dot"
        r={n.r}
        fill={color}
        stroke={selected ? "var(--color-text)" : "none"}
        strokeWidth={2}
      />
      {showLabel && (
        <text
          className="graph-label"
          x={lp.dx}
          y={lp.dy}
          textAnchor={lp.anchor}
          fontWeight={selected ? 600 : 400}
          style={{ pointerEvents: "none", paintOrder: "stroke", stroke: "var(--color-surface)", strokeWidth: 3 }}
        >
          {shorten(n.entity, labelFit(n.labelRoom, scale))}
        </text>
      )}
      {/* HTML in the picture, so the direction is Click UI's arrow icon, not a text glyph. */}
      {showRelation && n.via_relation && (
        <foreignObject x={-120} y={-n.r - 26} width={240} height={24} style={{ pointerEvents: "none" }}>
          <div className="graph-relation">
            <DirectionIcon direction={n.via_direction} />
            {relationLabel(n.via_relation)}
          </div>
        </foreignObject>
      )}
      {hidden > 0 ? (
        <g className="graph-badge" data-action="uncollapse" data-id={n.id} transform={`translate(${ax},${ay})`}>
          <rect
            x={-5}
            y={-10}
            width={badgeWidth(hidden)}
            height={20}
            rx={10}
            fill="var(--color-surface-3)"
            stroke="var(--color-border-strong)"
          />
          <text
            x={badgeWidth(hidden) / 2 - 5}
            y={5}
            textAnchor="middle"
            fill="var(--color-text)"
            style={{ pointerEvents: "none" }}
          >
            +{hidden}
          </text>
        </g>
      ) : (
        <g className="graph-expand" data-action="expand" data-id={n.id} transform={`translate(${ax},${ay})`}>
          <circle r={7} fill="var(--color-surface-3)" stroke="var(--color-border-strong)" />
          <path
            d="M-3.5 0H3.5M0 -3.5V3.5"
            stroke="var(--color-text)"
            strokeWidth={1.5}
            strokeLinecap="round"
            style={{ pointerEvents: "none" }}
          />
        </g>
      )}
    </g>
  );
});

/** The `data-id` and `data-action` of the element under a pointer event. */
function targetAt(target: EventTarget | null): { id: string | null; action: string | null } {
  if (!(target instanceof Element)) return { id: null, action: null };
  const act = target.closest("[data-action]");
  if (act) return { id: act.getAttribute("data-id"), action: act.getAttribute("data-action") };
  const el = target.closest("[data-id]");
  return { id: el ? el.getAttribute("data-id") : null, action: null };
}

export interface CanvasProps {
  model: WalkModel | null;
  visible: VisibleTree | null;
  mode: "walk" | "path";
  /** Path mode's answer, drawn or stated. */
  pathRes: PathResponse | null;
  loading: boolean;
  loadingLabel: string;
  error: string | null;
  seedTyped: string;
  selection: Selection;
  pinned: Set<string>;
  labels: LabelMode;
  typed: boolean;
  asOf: string;
  hasCommunities: boolean;
  communityLabel: (cid: number | null) => string;
  /** Empty state: the taxonomy's top entities as starting points; null while loading. */
  starters: TaxonomyEntry[] | null;
  taxonomyEmpty: boolean;
  onWalk: (entity: string) => void;
  onSelect: (sel: Selection) => void;
  onExpand: (entity: string) => void;
  onUncollapse: (entity: string) => void;
}

export function Canvas({
  model,
  visible,
  mode,
  pathRes,
  loading,
  loadingLabel,
  error,
  seedTyped,
  selection,
  pinned,
  labels,
  typed,
  asOf,
  hasCommunities,
  communityLabel,
  starters,
  taxonomyEmpty,
  onWalk,
  onSelect,
  onExpand,
  onUncollapse,
}: CanvasProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const readoutRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  // Canvas text floor: the svg scales with its viewBox, so the card carries
  // `--canvas-scale` = svg units per screen pixel and the stylesheet multiplies
  // --canvas-label-min by it. One DOM write per resize, none on hover; the layout itself
  // never reads it. The label lengths do (`labelFit`), from a copy in 0.05 steps, so a
  // resize re-renders the nodes only when a label could change length.
  const [scale, setScale] = useState(USUAL_SCALE);
  useEffect(() => {
    const card = cardRef.current;
    if (!card || typeof ResizeObserver === "undefined") return;
    const write = (w: number, h: number) => {
      const fit = Math.min(w / W, h / H);
      if (fit <= 0) return;
      const next = (1 / fit).toFixed(3);
      card.style.setProperty("--canvas-scale", next);
      setScale(Math.round(Number(next) * 20) / 20);
    };
    // No getBoundingClientRect on mount — it forced a layout (46 ms at 4x, the top
    // function of the Graph boot); the observer delivers the card's size on its first
    // callback.
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) write(e.contentRect.width, e.contentRect.height);
    });
    ro.observe(card);
    return () => ro.disconnect();
  }, []);
  const hotRef = useRef<string | null>(null);

  const placed = useMemo<Layout | null>(() => {
    if (!model || !visible) return null;
    return mode === "path" ? linearLayout(model, visible.nodes, W, H) : radialLayout(model, visible.nodes, W, H);
  }, [model, visible, mode]);
  const byEntity = useMemo(() => {
    const m = new Map<string, Placed>();
    if (placed) {
      m.set(placed.seed.entity, placed.seed);
      for (const n of placed.nodes) m.set(n.entity, n);
    }
    return m;
  }, [placed]);
  const byId = useMemo(() => {
    const m = new Map<string, Placed>();
    for (const n of placed?.nodes || []) m.set(n.id, n);
    return m;
  }, [placed]);
  const selectedEntity = selection?.kind === "node" ? selection.entity : null;
  const selectedEdge = selection?.kind === "edge" ? selection : null;
  // The selected node's chain, or the selected edge's child's chain, lit
  // while everything else dims (a hover, handled on the DOM, wins meanwhile).
  const selChain = useMemo(() => {
    if (!placed) return null;
    const target = selectedEntity ?? selectedEdge?.child ?? null;
    if (!target) return null;
    const n = byEntity.get(target);
    return n && n.id !== "s" ? new Set<string>(n.path) : null;
  }, [selectedEntity, selectedEdge, placed, byEntity]);

  // ---- Hover: attributes on the DOM, never state.
  const light = useCallback(
    (id: string | null) => {
      const svg = svgRef.current;
      if (!svg || hotRef.current === id) return;
      hotRef.current = id;
      svg.querySelectorAll("[data-hot]").forEach((el) => el.removeAttribute("data-hot"));
      const isEdge = id !== null && id.startsWith("e");
      const n = id ? byId.get(isEdge ? id.slice(1) : id) : undefined;
      const readout = readoutRef.current;
      if (!n || !placed) {
        svg.removeAttribute("data-hover");
        if (readout) readout.hidden = true;
        return;
      }
      if (readout) {
        const box = svg.getBoundingClientRect();
        const scale = Math.min(box.width / W, box.height / H);
        const parent = byEntity.get(n.parent ?? placed.seed.entity) ?? placed.seed;
        const px = (box.width - W * scale) / 2 + (isEdge ? (n.x + parent.x) / 2 : n.x) * scale;
        const py = (box.height - H * scale) / 2 + (isEdge ? (n.y + parent.y) / 2 : n.y) * scale;
        const above = py > 48;
        readout.textContent = isEdge
          ? `${shorten(parent.entity, 24)} — ${shorten(n.entity, 24)} · ${edgeText(n, typed)}${window_(n) ? ` · ${window_(n)}` : ""}`
          : `${n.entity} · hop ${n.hop} · ${edgeText(n, typed)}${hasCommunities ? ` · ${communityLabel(n.community_id)}` : ""}`;
        readout.style.left = `${Math.round(px)}px`;
        readout.style.top = `${Math.round(above ? py - (isEdge ? 0 : n.r * scale) - 8 : py + (isEdge ? 0 : n.r * scale) + 8)}px`;
        readout.dataset.side = above ? "above" : "below";
        readout.hidden = false;
      }
      const ids: string[] = ['[data-id="s"]'];
      for (const e of n.path) {
        const m = byEntity.get(e);
        if (m && m.id !== "s") ids.push(`[data-id="${m.id}"]`, `[data-id="e${m.id}"]`);
      }
      svg.querySelectorAll(ids.join(",")).forEach((el) => el.setAttribute("data-hot", ""));
      svg.setAttribute("data-hover", "");
    },
    [byId, byEntity, placed, typed, hasCommunities, communityLabel],
  );
  // A new picture forgets the old hover.
  useEffect(() => {
    hotRef.current = null;
    svgRef.current?.removeAttribute("data-hover");
    if (readoutRef.current) readoutRef.current.hidden = true;
  }, [placed]);

  const onOver = useCallback(
    (e: ReactMouseEvent<SVGSVGElement>) => {
      const { id } = targetAt(e.target);
      if (id === "s" || id === null) return;
      light(id); // an edge's id already carries its "e" prefix
    },
    [light],
  );
  const onOut = useCallback(
    (e: ReactMouseEvent<SVGSVGElement>) => {
      const from = targetAt(e.target).id;
      if (from === null) return;
      const to = targetAt(e.relatedTarget).id;
      if (to !== from) light(null);
    },
    [light],
  );
  const onLeave = useCallback(() => light(null), [light]);
  // Single click selects (toggles) a node or an edge, or acts on an
  // affordance; the second click of a double-click is ignored.
  const onClick = useCallback(
    (e: ReactMouseEvent<SVGSVGElement>) => {
      if (e.detail > 1) return;
      const { id, action } = targetAt(e.target);
      if (id === null) {
        onSelect(null);
        return;
      }
      if (action && id) {
        const n = id === "s" ? placed?.seed : byId.get(id);
        if (!n) return;
        if (action === "expand") onExpand(n.entity);
        else if (action === "uncollapse") onUncollapse(n.entity);
        return;
      }
      if (id.startsWith("e")) {
        const n = byId.get(id.slice(1));
        if (!n || !placed) return;
        const parent = n.parent ?? placed.seed.entity;
        onSelect(selectedEdge && selectedEdge.child === n.entity ? null : { kind: "edge", parent, child: n.entity });
        return;
      }
      const n = id === "s" ? placed?.seed : byId.get(id);
      if (!n) return;
      onSelect(selectedEntity === n.entity ? null : { kind: "node", entity: n.entity });
    },
    [byId, placed, onSelect, onExpand, onUncollapse, selectedEntity, selectedEdge],
  );
  const onDoubleClick = useCallback(
    (e: ReactMouseEvent<SVGSVGElement>) => {
      const { id, action } = targetAt(e.target);
      if (action) return;
      const n = id && id !== "s" ? byId.get(id) : undefined;
      if (n) onWalk(n.entity);
    },
    [byId, onWalk],
  );

  const showPicture = !loading && !error && placed && model && visible && visible.nodes.length > 0;
  const noPath = mode === "path" && !loading && !error && pathRes && !pathRes.found;
  const seedHidden = placed?.seed.entity ? pinned.has(placed.seed.entity) : false;
  // An empty state sits in the card's flow and the card stops clipping, so on a phone
  // the card grows past its aspect ratio to fit the copy instead of scrolling inside it.
  const emptyShown =
    (!loading && !error && mode === "walk" && ((!model && !seedTyped) || (model && visible?.nodes.length === 0))) ||
    Boolean(noPath && pathRes);

  return (
    <div
      ref={cardRef}
      className={`relative rounded-xl border border-border bg-surface aspect-[820/560] max-h-[560px] w-full ${emptyShown ? "flex flex-col" : "overflow-hidden"}`}
    >
      {loading && (
        <LoadingSkeleton label={loadingLabel} className="absolute inset-0 grid place-items-center">
          <div className="flex flex-col items-center gap-3">
            <Skeleton className="w-40 h-40 rounded-full" />
            <Skeleton className="h-3 w-32" />
          </div>
        </LoadingSkeleton>
      )}
      {error && <div className="absolute inset-0 grid place-items-center p-6 text-sm text-danger">{error}</div>}
      {!loading && !error && !model && mode === "walk" && !seedTyped && (
        <div className="flex-1 grid place-items-center">
          {taxonomyEmpty ? (
            <EmptyState
              icon="share-network"
              title="No entities yet"
              description="The graph fills as memories with entities are written (and, with the chunk extractor on, as documents are ingested)."
            />
          ) : (
            <div className="flex flex-col items-center text-center px-6 py-8">
              <div className="mb-5 grid place-items-center w-14 h-14 rounded-2xl bg-surface-2 text-text-muted">
                <Icon name="share-network" size="lg" aria-hidden="true" />
              </div>
              <h2>Pick an entity to walk from</h2>
              <p className="mt-2 max-w-md text-sm text-text-muted leading-relaxed">
                Nothing is walked until you ask. Type an entity and choose Walk, type{" "}
                <span className="font-mono">a → b</span> for a path, or start from one the memories mention most.
              </p>
              {starters === null ? (
                <LoadingSkeleton label="Loading starting points" className="mt-5">
                  <div className="flex flex-wrap justify-center gap-1.5">
                    {["w-20", "w-14", "w-24", "w-16", "w-12", "w-20"].map((w, i) => (
                      <Skeleton key={i} className={`h-6 rounded-full ${w}`} />
                    ))}
                  </div>
                </LoadingSkeleton>
              ) : (
                <div className="mt-5 flex flex-wrap justify-center gap-1.5 max-w-lg">
                  {starters.map((e) => (
                    <Chip
                      key={e.value}
                      label={e.value}
                      count={e.count}
                      ariaLabel={e.value}
                      onClick={() => onWalk(e.value)}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
      {!loading && !error && mode === "walk" && model && visible && visible.nodes.length === 0 && (
        <div className="flex-1 grid place-items-center">
          <EmptyState
            icon="share-network"
            title={
              model.nodes.size > 0
                ? `Every edge from ${model.seed} is filtered out`
                : `No connections from ${model.seed}`
            }
            description={
              model.nodes.size > 0
                ? "The relation or provenance chips on the left hide every edge of this walk. Clear a chip, or pin a node to keep it through the filter."
                : typed
                  ? "No typed triple has this entity as subject or object. Untick “typed edges only” to walk co-mentions, or pick one of the matches above."
                  : asOf
                    ? `No edge touching this entity was valid on ${asOf}. Clear the date, or pick one of the matches above.`
                    : "No memory mentions this entity together with another. Pick one of the matches above, or a most-mentioned entity on the left."
            }
          />
        </div>
      )}
      {noPath && pathRes && (
        <div className="flex-1 grid place-items-center">
          <EmptyState
            icon="arrow-directions"
            title={`No path within ${pathRes.hops} hop${pathRes.hops === 1 ? "" : "s"}`}
            description={
              `${pathRes.entity_a} and ${pathRes.entity_b} are not connected within ${pathRes.hops} hop${pathRes.hops === 1 ? "" : "s"} on ` +
              `${typed ? "typed" : "co-mention"} edges${asOf ? ` valid on ${asOf}` : ""}.` +
              (pathRes.truncated
                ? " The search was trimmed on the way (per-hop top-k or the hop budget); more hops or a larger per-hop may find one."
                : " The search was not trimmed: within this budget there is none.")
            }
          />
        </div>
      )}
      {showPicture && (
        // biome-ignore lint/a11y/useKeyWithMouseEvents: hover only lights the picture, which is aria-hidden; keyboard users walk the same nodes in the outline tree
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className="graph-svg absolute inset-0 w-full h-full select-none"
          aria-hidden="true"
          data-sel-mode={selChain ? "" : undefined}
          data-linear={placed.linear ? "" : undefined}
          data-by-hop={hasCommunities ? undefined : ""}
          onMouseOver={onOver}
          onMouseOut={onOut}
          onMouseLeave={onLeave}
          onClick={onClick}
          onDoubleClick={onDoubleClick}
        >
          {placed.rings.map((r, i) => (
            <g key={`ring-${i}`}>
              <circle cx={W / 2} cy={H / 2} r={r} fill="none" stroke="var(--color-border)" strokeDasharray="3 5" />
              <text
                x={W / 2 - r * 0.7071 - 6}
                y={H / 2 - r * 0.7071 - 6}
                textAnchor="end"
                fill="var(--color-text-muted)"
                style={{ paintOrder: "stroke", stroke: "var(--color-surface)", strokeWidth: 3 }}
              >
                {i + 1} hop{i === 0 ? "" : "s"}
              </text>
            </g>
          ))}
          {placed.nodes.map((n) => {
            const parent = byEntity.get(n.parent ?? placed.seed.entity) ?? placed.seed;
            const edgeSel = selectedEdge
              ? selectedEdge.child === n.entity
              : selChain
                ? selChain.has(n.entity) && selChain.has(parent.entity)
                : false;
            return (
              <GraphEdge
                key={`e-${n.entity}`}
                id={n.id}
                x1={parent.x}
                y1={parent.y}
                x2={n.x}
                y2={n.y}
                weight={n.via_co_occur}
                sel={edgeSel}
                note={placed.linear ? edgeText(n, Boolean(n.via_relation)) : undefined}
                note2={placed.linear ? window_(n) || undefined : undefined}
              />
            );
          })}
          {placed.nodes.map((n) => (
            <GraphNode
              key={n.entity}
              n={n}
              color={colorFor(n, hasCommunities)}
              showLabel={
                placed.linear || labels === "all" || (labels === "key" && (n.keyLabel || selectedEntity === n.entity))
              }
              showRelation={!placed.linear && typed && labels === "all"}
              selected={selectedEntity === n.entity}
              sel={selChain ? selChain.has(n.entity) : false}
              pinned={pinned.has(n.entity)}
              hidden={visible.hiddenUnder.get(n.entity) || 0}
              scale={scale}
            />
          ))}
          <g
            className="graph-node cursor-pointer"
            data-id="s"
            data-sel={selChain ? "" : undefined}
            data-selected={selectedEntity === placed.seed.entity ? "" : undefined}
            transform={`translate(${placed.seed.x},${placed.seed.y})`}
          >
            <circle r={Math.max(12, placed.seed.r + 4)} fill="transparent" />
            {seedHidden && (
              <circle
                r={placed.seed.r + 4}
                fill="none"
                stroke="var(--color-accent)"
                strokeWidth={1.5}
                strokeDasharray="3 2"
              />
            )}
            <circle
              className="graph-dot"
              r={placed.seed.r}
              fill={colorFor(placed.seed, hasCommunities)}
              stroke="var(--color-text)"
              strokeWidth={2}
            />
            <text
              className="graph-label graph-label-seed"
              y={placed.seed.r + 16}
              textAnchor="middle"
              style={{ pointerEvents: "none", paintOrder: "stroke", stroke: "var(--color-surface)", strokeWidth: 3 }}
            >
              {shorten(placed.seed.entity, labelFit(placed.seed.labelRoom, scale))}
            </text>
            {(visible.hiddenUnder.get(placed.seed.entity) || 0) > 0 ? (
              <g
                className="graph-badge"
                data-action="uncollapse"
                data-id="s"
                transform={`translate(${placed.seed.r + 7},${-placed.seed.r - 7})`}
              >
                <rect
                  x={-5}
                  y={-10}
                  width={badgeWidth(visible.hiddenUnder.get(placed.seed.entity) || 0)}
                  height={20}
                  rx={10}
                  fill="var(--color-surface-3)"
                  stroke="var(--color-border-strong)"
                />
                <text
                  x={badgeWidth(visible.hiddenUnder.get(placed.seed.entity) || 0) / 2 - 5}
                  y={5}
                  textAnchor="middle"
                  fill="var(--color-text)"
                  style={{ pointerEvents: "none" }}
                >
                  +{visible.hiddenUnder.get(placed.seed.entity)}
                </text>
              </g>
            ) : (
              <g
                className="graph-expand"
                data-action="expand"
                data-id="s"
                transform={`translate(${placed.seed.r + 7},${-placed.seed.r - 7})`}
              >
                <circle r={7} fill="var(--color-surface-3)" stroke="var(--color-border-strong)" />
                <path
                  d="M-3.5 0H3.5M0 -3.5V3.5"
                  stroke="var(--color-text)"
                  strokeWidth={1.5}
                  strokeLinecap="round"
                  style={{ pointerEvents: "none" }}
                />
              </g>
            )}
          </g>
        </svg>
      )}
      {showPicture && <div ref={readoutRef} hidden className="graph-readout" aria-hidden="true" />}
    </div>
  );
}
