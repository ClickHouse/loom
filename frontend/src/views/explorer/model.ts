import type { PathResponse, RelatedEntity, TaxonomyEntry, TraverseResponse, TraversalNode } from "../../api";

/**
 * The Explorer's walk model: one normalised tree, seed first, that a `facts.traverse`
 * result seeds and `facts.related` extends in place. Every node hangs off exactly one
 * parent (the node before it on its path), is reported once (a global visited set), and
 * the whole tree never holds more than `NODE_CAP` entities — an expand that would cross
 * the cap keeps the strongest new children and says so, the way the server does for a
 * walk.
 *
 * Facets (relation / provenance chips), collapse and pin are view state
 * applied over the model by `visibleNodes`, never written into it: a
 * facet change filters what is drawn, a pinned node survives the filter
 * with its ancestors, a collapsed node hides its descendants and shows
 * their count. Changing any of them never touches the selection.
 */

export const NODE_CAP = 200;

export type Provenance = "extracted" | "inferred" | "facet";
export const PROVENANCE_TIERS: Provenance[] = ["extracted", "inferred", "facet"];
export function isProvenance(s: string): s is Provenance {
  return s === "extracted" || s === "inferred" || s === "facet";
}

export interface XNode {
  entity: string;
  hop: number;
  via_co_occur: number;
  via_relation: string;
  via_direction: string;
  via_provenance: string;
  valid_from: string;
  valid_to: string;
  community_id: number | null;
  /** Chain from the seed to this node, seed first. */
  path: string[];
  /** The node before it on the path; null for the seed only. */
  parent: string | null;
  /** How the node entered the tree: the walk, or an expand in place. */
  origin: "walk" | "expand";
  /** Reported but not expanded by the hub policy. */
  hub?: boolean;
}

export interface WalkModel {
  seed: string;
  seedCommunityId: number | null;
  /** Insertion order = hop order for the walk, then expands appended. */
  nodes: Map<string, XNode>;
  /** The server trimmed the walk or an expand hit the cap. */
  truncated: boolean;
}

export type Selection = { kind: "node"; entity: string } | { kind: "edge"; parent: string; child: string } | null;

export interface EdgeFilters {
  /** Empty = every relation. */
  relations: Set<string>;
  /** Empty = every tier. */
  provenance: Set<Provenance>;
}

export function emptyFilters(): EdgeFilters {
  return { relations: new Set(), provenance: new Set() };
}

export function hasEdgeFilters(f: EdgeFilters): boolean {
  return f.relations.size > 0 || f.provenance.size > 0;
}

function parentFromPath(n: TraversalNode, seed: string): string {
  const p = n.path || [];
  return p.length >= 2 ? p[p.length - 2] : seed;
}

/** A `facts.traverse` result as a model. The seed is the tree's root and is
 *  not in `nodes` (the server does not echo it as a hop-0 row). */
export function fromTraverse(res: TraverseResponse): WalkModel {
  const nodes = new Map<string, XNode>();
  for (const n of res.nodes) {
    if (n.entity === res.seed || nodes.has(n.entity)) continue;
    nodes.set(n.entity, {
      entity: n.entity,
      hop: n.hop,
      via_co_occur: n.via_co_occur,
      via_relation: n.via_relation || "",
      via_direction: n.via_direction || "",
      via_provenance: n.via_provenance || "",
      valid_from: n.valid_from || "",
      valid_to: n.valid_to || "",
      community_id: n.community_id ?? null,
      path: n.path && n.path.length ? n.path : [res.seed, n.entity],
      parent: parentFromPath(n, res.seed),
      origin: "walk",
      hub: Boolean(n.hub),
    });
  }
  return { seed: res.seed, seedCommunityId: res.seed_community_id ?? null, nodes, truncated: Boolean(res.truncated) };
}

/** A found `facts.path` as a model: one chain, `entity_a` the root, each
 *  node hanging off the one before it, so the outline and the inspector
 *  read a path exactly as they read a walk. Null when no path was found. */
export function fromPath(res: PathResponse): WalkModel | null {
  if (!res.found || res.path.length < 2) return null;
  const nodes = new Map<string, XNode>();
  for (let i = 1; i < res.path.length; i++) {
    const e = res.edges[i - 1];
    const entity = res.path[i];
    if (nodes.has(entity) || entity === res.path[0]) continue;
    nodes.set(entity, {
      entity,
      hop: i,
      via_co_occur: e?.co_occur ?? 0,
      via_relation: e?.relation || "",
      via_direction: e?.direction || "",
      via_provenance: "",
      valid_from: e?.valid_from || "",
      valid_to: e?.valid_to || "",
      community_id: null,
      path: res.path.slice(0, i + 1),
      parent: res.path[i - 1],
      origin: "walk",
    });
  }
  return { seed: res.path[0], seedCommunityId: null, nodes, truncated: false };
}

interface ExpandResult {
  model: WalkModel;
  /** Entities appended under the parent, strongest first. */
  added: string[];
  /** Neighbours the server returned that the tree already held. */
  known: number;
  /** True when the 200-node cap cut the new children short. */
  capped: boolean;
}

/** Append one hop of neighbours under `parent` (expand in place). Entities already in the
 *  tree are skipped — the visited set is global, so the picture stays a tree — and the
 *  cap keeps the strongest. */
export function expandModel(m: WalkModel, parent: string, related: RelatedEntity[], perHop: number): ExpandResult {
  const parentNode = parent === m.seed ? null : m.nodes.get(parent);
  if (parent !== m.seed && !parentNode) return { model: m, added: [], known: 0, capped: false };
  const hop = parentNode ? parentNode.hop + 1 : 1;
  const path = parentNode ? parentNode.path : [m.seed];
  const fresh = [...related]
    .filter((r) => r.entity !== m.seed && !m.nodes.has(r.entity))
    .sort((a, b) => b.co_occur - a.co_occur)
    .slice(0, perHop);
  const known = related.length - related.filter((r) => r.entity !== m.seed && !m.nodes.has(r.entity)).length;
  const room = Math.max(0, NODE_CAP - m.nodes.size);
  const kept = fresh.slice(0, room);
  const capped = kept.length < fresh.length;
  if (kept.length === 0) return { model: capped ? { ...m, truncated: true } : m, added: [], known, capped };
  const nodes = new Map(m.nodes);
  for (const r of kept) {
    nodes.set(r.entity, {
      entity: r.entity,
      hop,
      via_co_occur: r.co_occur,
      via_relation: "",
      via_direction: r.role === "subject" ? "out" : r.role === "object" ? "in" : "",
      via_provenance: "",
      valid_from: "",
      valid_to: "",
      community_id: null,
      path: [...path, r.entity],
      parent,
      origin: "expand",
    });
  }
  return { model: { ...m, nodes, truncated: m.truncated || capped }, added: kept.map((r) => r.entity), known, capped };
}

/** Whether an edge passes the relation / provenance chips. An edge with
 *  no relation (a co-mention) fails a relation filter; one with no
 *  provenance fails a provenance filter — the chips describe typed edges. */
function edgePasses(n: XNode, f: EdgeFilters): boolean {
  if (f.relations.size && !f.relations.has(n.via_relation)) return false;
  if (f.provenance.size && !(isProvenance(n.via_provenance) && f.provenance.has(n.via_provenance))) return false;
  return true;
}

export interface VisibleTree {
  /** Nodes drawn, in the model's order. */
  nodes: XNode[];
  /** For a collapsed node still drawn: how many descendants it hides. */
  hiddenUnder: Map<string, number>;
  /** How many nodes the chips removed (pinned ones excepted). */
  filteredOut: number;
}

/** The tree after facets, collapse and pins. A node is drawn when its
 *  whole chain passes the chips (or it is pinned, which also keeps its
 *  ancestors) and no ancestor is collapsed. */
export function visibleNodes(m: WalkModel, f: EdgeFilters, collapsed: Set<string>, pinned: Set<string>): VisibleTree {
  const keep = new Set<string>();
  // Pinned nodes and their ancestors bypass the chips.
  for (const p of pinned) {
    const n = m.nodes.get(p);
    if (!n) continue;
    for (const e of n.path) if (e !== m.seed) keep.add(e);
  }
  const passes = new Map<string, boolean>();
  const passChain = (n: XNode): boolean => {
    const cached = passes.get(n.entity);
    if (cached !== undefined) return cached;
    let ok = keep.has(n.entity) || edgePasses(n, f);
    if (ok && n.parent && n.parent !== m.seed) {
      const p = m.nodes.get(n.parent);
      ok = p ? passChain(p) : false;
    }
    passes.set(n.entity, ok);
    return ok;
  };
  const hiddenUnder = new Map<string, number>();
  const out: XNode[] = [];
  let filteredOut = 0;
  for (const n of m.nodes.values()) {
    if (!passChain(n)) {
      filteredOut++;
      continue;
    }
    // An ancestor collapsed: count it under the nearest collapsed ancestor.
    const anc = n.path.slice(1, -1).find((e) => collapsed.has(e));
    if (anc !== undefined) {
      hiddenUnder.set(anc, (hiddenUnder.get(anc) || 0) + 1);
      continue;
    }
    if (collapsed.has(m.seed) && n.hop >= 1) {
      hiddenUnder.set(m.seed, (hiddenUnder.get(m.seed) || 0) + 1);
      continue;
    }
    out.push(n);
  }
  return { nodes: out, hiddenUnder, filteredOut };
}

/** Children by parent, each list strongest first. */
export function childrenIndex(nodes: XNode[]): Map<string, XNode[]> {
  const children = new Map<string, XNode[]>();
  for (const n of nodes) {
    const p = n.parent ?? "";
    children.set(p, [...(children.get(p) || []), n]);
  }
  for (const list of children.values()) list.sort((a, b) => b.via_co_occur - a.via_co_occur);
  return children;
}

/** Depth-first tree order from the seed, for the outline. */
export function outlineOrder(seed: string, children: Map<string, XNode[]>): XNode[] {
  const out: XNode[] = [];
  const seen = new Set<string>();
  const visit = (entity: string) => {
    for (const c of children.get(entity) || []) {
      if (seen.has(c.entity)) continue;
      seen.add(c.entity);
      out.push(c);
      visit(c.entity);
    }
  };
  visit(seed);
  return out;
}

/** The relations the model's typed edges carry, most frequent first. */
export function relationsIn(m: WalkModel | null): Array<{ relation: string; count: number }> {
  if (!m) return [];
  const counts = new Map<string, number>();
  for (const n of m.nodes.values())
    if (n.via_relation) counts.set(n.via_relation, (counts.get(n.via_relation) || 0) + 1);
  return [...counts.entries()].map(([relation, count]) => ({ relation, count })).sort((a, b) => b.count - a.count);
}

/** Path syntax in the omnibox: `a → b`, `a -> b`. Null when the text is a
 *  single seed. Both ends are lower-cased the way the server canonicalises. */
export function parsePathQuery(text: string): { a: string; b: string } | null {
  const m = /^(.+?)\s*(?:→|->)\s*(.+)$/.exec(text.trim());
  if (!m) return null;
  const a = m[1].trim().toLowerCase();
  const b = m[2].trim().toLowerCase();
  return a && b && a !== b ? { a, b } : null;
}

/** Entities matching a typed query: prefix matches first, then substring matches, each
 *  group in the taxonomy's order, at most `cap`. An empty query matches nothing. */
export function matchEntities(entities: TaxonomyEntry[], q: string, cap: number): TaxonomyEntry[] {
  if (!q) return [];
  const pre = entities.filter((e) => e.value.startsWith(q));
  const sub = entities.filter((e) => !e.value.startsWith(q) && e.value.includes(q));
  return [...pre, ...sub].slice(0, cap);
}

export function relationLabel(relation: string): string {
  return relation.replace(/_/g, " ");
}

export function shorten(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

/** Today as YYYY-MM-DD, for the as-of input's upper bound. */
export function today(): string {
  return new Date().toISOString().slice(0, 10);
}

/** An as-of day as the instant the server replays: the end of that day. */
export function asOfInstant(asOf: string): string | undefined {
  return asOf ? `${asOf}T23:59:59` : undefined;
}
