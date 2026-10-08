import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { GitBranch } from "lucide-react";
import { EmptyState } from "../../../components/EmptyState";
import { Skeleton } from "../../../components/Skeleton";
import { shorten } from "../model";
import type { TrackerEdge, TrackerGraph as TrackerGraphModel, TrackerNode } from "./model";

/**
 * TrackerGraph — the selected issue's neighbourhood in the tracker graph, drawn with the
 * Explorer canvas's technique: a fixed `aspect-[820/560]` card, `data-hover` on the svg
 * and `data-hot` on the lit nodes and edges written by one delegated handler, one always-
 * present readout, memoised nodes and edges, a ≥ 12 px hit target, and no React state on
 * hover.
 *
 * Layout is a tree from the seed: the PR the graph names as the cause to
 * the left, the fixing PRs to the right, duplicates below; every edge is
 * typed (`data-rel`), directed (an arrowhead) and labelled. Issues are
 * circles, PRs rounded squares. A culprit PR carries the "named by graph"
 * mark.
 */

const W = 820;
const H = 560;

interface Placed extends TrackerNode {
  x: number;
  y: number;
  r: number /** label characters that fit between the node and the card's edge */;
  labelMax: number;
}
interface PlacedEdge extends TrackerEdge {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}
interface Layout {
  seed: Placed;
  nodes: Placed[];
  edges: PlacedEdge[];
}

function spread(count: number, centre: number, span: number, i: number): number {
  if (count <= 1) return centre;
  const step = Math.min(90, span / (count - 1));
  return centre - (step * (count - 1)) / 2 + step * i;
}

function layout(g: TrackerGraphModel): Layout {
  const cx = W / 2;
  const cy = H / 2 - 30;
  const seed: Placed = { ...g.seed, x: cx, y: cy, r: 16, labelMax: 30 };
  const causes = g.nodes.filter((n) => n.role === "cause");
  const fixes = g.nodes.filter((n) => n.role === "fix" || n.role === "related");
  const dups = g.nodes.filter((n) => n.role === "duplicate" || n.role === "linked");
  const placed: Placed[] = [];
  // Side columns sit 240 units in so an outward label of ~24 characters
  // (≈ 9 units each at the canvas floor) ends before the card's edge — the
  // edge of the card is never a silent clip.
  const side = 240;
  const fit = (room: number) => Math.max(8, Math.min(30, Math.floor(room / 9)));
  causes.forEach((n, i) =>
    placed.push({ ...n, x: side, y: spread(causes.length, cy, 380, i), r: 12, labelMax: fit(side - 12 - 10) }),
  );
  fixes.forEach((n, i) =>
    placed.push({ ...n, x: W - side, y: spread(fixes.length, cy, 380, i), r: 12, labelMax: fit(side - 12 - 10) }),
  );
  dups.forEach((n, i) =>
    placed.push({
      ...n,
      x: spread(dups.length, cx, 520, i),
      y: H - 90,
      r: 11,
      labelMax: fit(Math.min(120, W / Math.max(1, dups.length)) - 8),
    }),
  );
  const by = new Map<string, Placed>([[seed.id, seed], ...placed.map((p) => [p.id, p] as [string, Placed])]);
  const edges: PlacedEdge[] = g.edges.flatMap((e) => {
    const a = by.get(e.from);
    const b = by.get(e.to);
    return a && b ? [{ ...e, x1: a.x, y1: a.y, x2: b.x, y2: b.y }] : [];
  });
  return { seed, nodes: placed, edges };
}

const REL_LABEL: Record<TrackerEdge["rel"], string> = {
  caused_by: "caused",
  fixed_by: "fixes",
  duplicate_of: "duplicate of",
  related_to: "related to",
  fixes: "fixes",
  caused: "caused",
};

/** Edge stroke width, read by `.graph-edge-line` as the `--w` custom property. */
const EDGE_STYLE: CSSProperties & { "--w": number } = { "--w": 1.5 };

const Edge = memo(function Edge({ e, sel }: { e: PlacedEdge; sel: boolean }) {
  // Shorten the line so the arrowhead ends at the target's rim.
  const dx = e.x2 - e.x1;
  const dy = e.y2 - e.y1;
  const len = Math.max(1, Math.hypot(dx, dy));
  const trim = 22;
  const x2 = e.x2 - (dx / len) * trim;
  const y2 = e.y2 - (dy / len) * trim;
  const x1 = e.x1 + (dx / len) * 20;
  const y1 = e.y1 + (dy / len) * 20;
  // Edges to the row below leave the seed almost in parallel, so their
  // labels sit nearer the target where the fan has opened ( two "related to" labels must
  // not print over each other).
  const f = e.rel === "duplicate_of" || e.rel === "related_to" ? 0.7 : 0.5;
  const mx = x1 + (x2 - x1) * f;
  const my = y1 + (y2 - y1) * f;
  return (
    <g
      className="graph-edge tracker-edge"
      data-id={`e${e.id}`}
      data-rel={e.rel}
      data-from={e.from}
      data-to={e.to}
      data-sel={sel ? "" : undefined}
    >
      <line className="graph-edge-hit" x1={x1} y1={y1} x2={x2} y2={y2} />
      <line
        className="graph-edge-line"
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        stroke="var(--color-border-strong)"
        markerEnd="url(#tracker-arrow)"
        style={EDGE_STYLE}
      />
      <text className="graph-edge-note" x={mx} y={my - 8} textAnchor="middle" fill="var(--color-text-muted)">
        {REL_LABEL[e.rel]}
      </text>
    </g>
  );
});

const Node = memo(function Node({ n, selected, sel }: { n: Placed; selected: boolean; sel: boolean }) {
  const label = n.known
    ? `#${n.number} ${shorten(n.title, Math.max(6, n.labelMax - String(n.number).length - 2))}`
    : `#${n.number}`;
  const below = n.role === "duplicate" || n.role === "linked" || n.role === "seed";
  return (
    <g
      className="graph-node cursor-pointer"
      data-id={n.id}
      data-role={n.role}
      data-kind={n.kind}
      data-selected={selected ? "" : undefined}
      data-sel={sel ? "" : undefined}
      transform={`translate(${n.x},${n.y})`}
    >
      <circle r={Math.max(12, n.r + 6)} fill="transparent" />
      {n.kind === "pr" ? (
        <rect
          className="graph-dot"
          x={-n.r}
          y={-n.r}
          width={n.r * 2}
          height={n.r * 2}
          rx={4}
          fill={n.role === "cause" ? "var(--color-danger)" : "var(--color-info)"}
          stroke={selected ? "var(--color-text)" : "none"}
          strokeWidth={2}
        />
      ) : (
        <circle
          className="graph-dot"
          r={n.r}
          fill={
            n.role === "seed"
              ? "var(--color-cat-1)"
              : n.role === "linked"
                ? "var(--color-border-strong)"
                : "var(--color-cat-3)"
          }
          stroke={selected || n.role === "seed" ? "var(--color-text)" : "none"}
          strokeWidth={2}
        />
      )}
      <text
        className={`graph-label ${n.role === "seed" ? "graph-label-seed" : ""}`}
        x={below ? 0 : n.role === "cause" ? -n.r - 6 : n.r + 6}
        y={below ? n.r + 16 : 4}
        textAnchor={below ? "middle" : n.role === "cause" ? "end" : "start"}
        style={{ pointerEvents: "none", paintOrder: "stroke", stroke: "var(--color-surface)", strokeWidth: 3 }}
      >
        {label}
      </text>
      {n.namedByGraph && (
        <text
          className="tracker-named"
          x={-n.r - 6}
          y={n.r + 14}
          textAnchor="end"
          fill="var(--color-danger)"
          style={{ pointerEvents: "none", paintOrder: "stroke", stroke: "var(--color-surface)", strokeWidth: 3 }}
        >
          named by graph
        </text>
      )}
    </g>
  );
});

function targetId(target: EventTarget | null): string | null {
  if (!(target instanceof Element)) return null;
  const el = target.closest("[data-id]");
  return el ? el.getAttribute("data-id") : null;
}

export interface TrackerGraphProps {
  graph: TrackerGraphModel | null;
  loading: boolean;
  error: string | null;
  /** Nothing selected yet: what to do. */
  hint: string;
  selected: number | null;
  onSelect: (n: number | null) => void;
  onOpen: (n: number) => void;
}

export function TrackerGraph({ graph, loading, error, hint, selected, onSelect, onOpen }: TrackerGraphProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const readoutRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const hotRef = useRef<string | null>(null);

  useEffect(() => {
    const card = cardRef.current;
    if (!card || typeof ResizeObserver === "undefined") return;
    const write = (w: number, h: number) => {
      const scale = Math.min(w / W, h / H);
      if (scale > 0) card.style.setProperty("--canvas-scale", (1 / scale).toFixed(3));
    };
    const r = card.getBoundingClientRect();
    write(r.width, r.height);
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) write(e.contentRect.width, e.contentRect.height);
    });
    ro.observe(card);
    return () => ro.disconnect();
  }, []);

  const placed = useMemo(() => (graph ? layout(graph) : null), [graph]);
  const byId = useMemo(() => {
    const m = new Map<string, Placed>();
    if (placed) {
      m.set(placed.seed.id, placed.seed);
      for (const n of placed.nodes) m.set(n.id, n);
    }
    return m;
  }, [placed]);
  const edgesOf = useMemo(() => {
    const m = new Map<string, PlacedEdge[]>();
    for (const e of placed?.edges || []) {
      m.set(e.from, [...(m.get(e.from) || []), e]);
      m.set(e.to, [...(m.get(e.to) || []), e]);
    }
    return m;
  }, [placed]);

  // Hover: attributes on the DOM, never state.
  const light = useCallback(
    (id: string | null) => {
      const svg = svgRef.current;
      if (!svg || hotRef.current === id) return;
      hotRef.current = id;
      svg.querySelectorAll("[data-hot]").forEach((el) => el.removeAttribute("data-hot"));
      const readout = readoutRef.current;
      const isEdge = id !== null && id.startsWith("e");
      const edge = isEdge && placed ? placed.edges.find((e) => `e${e.id}` === id) : undefined;
      const node = !isEdge && id ? byId.get(id) : undefined;
      // Where the readout anchors, what it says, the radius it clears, and what lights up.
      let px: number, py: number, text: string, r: number;
      const sels: string[] = [];
      if (edge) {
        px = (edge.x1 + edge.x2) / 2;
        py = (edge.y1 + edge.y2) / 2;
        text = `#${byId.get(edge.from)?.number ?? edge.from} ${REL_LABEL[edge.rel]} #${byId.get(edge.to)?.number ?? edge.to}`;
        r = 0;
        sels.push(`[data-id="e${edge.id}"]`, `[data-id="${edge.from}"]`, `[data-id="${edge.to}"]`);
      } else if (placed && node) {
        px = node.x;
        py = node.y;
        text = `${node.kind === "pr" ? "PR" : "issue"} #${node.number}${node.title ? ` · ${shorten(node.title, 60)}` : " · not in the tracker"}${node.severity ? ` · ${node.severity}` : ""}${node.namedByGraph ? " · named by the tracker graph as the cause" : ""}`;
        r = node.r;
        sels.push(`[data-id="${node.id}"]`);
        for (const e of edgesOf.get(node.id) || [])
          sels.push(`[data-id="e${e.id}"]`, `[data-id="${e.from}"]`, `[data-id="${e.to}"]`);
      } else {
        svg.removeAttribute("data-hover");
        if (readout) readout.hidden = true;
        return;
      }
      const box = svg.getBoundingClientRect();
      const scale = Math.min(box.width / W, box.height / H);
      const ox = (box.width - W * scale) / 2;
      const oy = (box.height - H * scale) / 2;
      if (readout) {
        const above = py * scale > 48;
        readout.textContent = text;
        readout.style.left = `${Math.round(ox + px * scale)}px`;
        readout.style.top = `${Math.round(oy + py * scale + (above ? -r * scale - 8 : r * scale + 8))}px`;
        readout.dataset.side = above ? "above" : "below";
        readout.hidden = false;
      }
      svg.querySelectorAll(sels.join(",")).forEach((el) => el.setAttribute("data-hot", ""));
      svg.setAttribute("data-hover", "");
    },
    [placed, byId, edgesOf],
  );
  useEffect(() => {
    hotRef.current = null;
    svgRef.current?.removeAttribute("data-hover");
    if (readoutRef.current) readoutRef.current.hidden = true;
  }, [placed]);

  const onOver = useCallback(
    (e: ReactMouseEvent<SVGSVGElement>) => {
      const id = targetId(e.target);
      if (id !== null) light(id);
    },
    [light],
  );
  const onOut = useCallback(
    (e: ReactMouseEvent<SVGSVGElement>) => {
      const from = targetId(e.target);
      if (from === null) return;
      if (targetId(e.relatedTarget) !== from) light(null);
    },
    [light],
  );
  const onLeave = useCallback(() => light(null), [light]);
  const onClick = useCallback(
    (e: ReactMouseEvent<SVGSVGElement>) => {
      if (e.detail > 1) return;
      const id = targetId(e.target);
      if (id === null || id.startsWith("e")) {
        onSelect(null);
        return;
      }
      const n = byId.get(id);
      if (n) onSelect(selected === n.number ? null : n.number);
    },
    [byId, onSelect, selected],
  );
  const onDoubleClick = useCallback(
    (e: ReactMouseEvent<SVGSVGElement>) => {
      const id = targetId(e.target);
      const n = id && !id.startsWith("e") ? byId.get(id) : undefined;
      if (n && n.known) onOpen(n.number);
    },
    [byId, onOpen],
  );

  const selChain = useMemo(() => {
    if (!placed || selected === null) return null;
    const n = [...byId.values()].find((p) => p.number === selected);
    if (!n) return null;
    const ids = new Set<string>([n.id]);
    for (const e of edgesOf.get(n.id) || []) {
      ids.add(`e${e.id}`);
      ids.add(e.from);
      ids.add(e.to);
    }
    return ids;
  }, [placed, selected, byId, edgesOf]);

  const show = !loading && !error && placed;
  return (
    <div
      ref={cardRef}
      className="relative rounded-xl border border-border bg-surface overflow-hidden aspect-[820/560] max-h-[560px] w-full"
    >
      {loading && (
        <div className="absolute inset-0 grid place-items-center" role="status" aria-label="Reading the tracker graph">
          <div className="flex flex-col items-center gap-3">
            <Skeleton className="w-40 h-40 rounded-full" />
            <Skeleton className="h-3 w-32" />
          </div>
        </div>
      )}
      {error && <div className="absolute inset-0 grid place-items-center p-6 text-sm text-danger">{error}</div>}
      {!loading && !error && !placed && (
        <div className="absolute inset-0 overflow-auto grid place-items-center">
          <EmptyState icon={GitBranch} title="Pick an issue to see its graph" description={hint} />
        </div>
      )}
      {show && (
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className="graph-svg tracker-graph-svg absolute inset-0 w-full h-full select-none"
          aria-hidden="true"
          data-sel-mode={selChain ? "" : undefined}
          onMouseOver={onOver}
          onMouseOut={onOut}
          onMouseLeave={onLeave}
          onClick={onClick}
          onDoubleClick={onDoubleClick}
        >
          <defs>
            <marker
              id="tracker-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="8"
              markerHeight="8"
              orient="auto-start-reverse"
            >
              <path d="M0 0L10 5L0 10z" fill="var(--color-border-strong)" />
            </marker>
          </defs>
          {placed.nodes.length === 0 && (
            <text x={W / 2} y={H / 2 + 60} textAnchor="middle" fill="var(--color-text-muted)">
              no caused_by, fixes, duplicate or related edge recorded for this issue
            </text>
          )}
          {placed.edges.map((e) => (
            <Edge key={e.id} e={e} sel={selChain ? selChain.has(`e${e.id}`) : false} />
          ))}
          {placed.nodes.map((n) => (
            <Node key={n.id} n={n} selected={selected === n.number} sel={selChain ? selChain.has(n.id) : false} />
          ))}
          <Node
            n={placed.seed}
            selected={selected === placed.seed.number}
            sel={selChain ? selChain.has(placed.seed.id) : false}
          />
          <text x={12} y={H - 14} fill="var(--color-text-subtle)">
            ← caused by · fixed by → · duplicates and related below
          </text>
        </svg>
      )}
      {show && <div ref={readoutRef} hidden className="graph-readout" aria-hidden="true" />}
    </div>
  );
}
