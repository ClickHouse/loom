import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Link } from "@clickhouse/click-ui/Link";
import {
  getTaxonomy,
  pathFacts,
  trackerBinding,
  relatedFacts,
  traverseFacts,
  type Identity,
  type PathResponse,
  type TrackerBinding,
  type TaxonomyResponse,
} from "../../api";
import { BackLink } from "../../components/BackLink";
import { MemoryInspector } from "../../components/MemoryInspector";
import { toast } from "../../components/Toast";
import { Loading } from "../../components/ui";
import { useAbortableEffect } from "../../lib/hooks";
import { humanizeError } from "../../lib/errorWords";
import { isRecord } from "../../lib/guards";
import { useIdentity } from "../../lib/state";
import { Bar } from "./Bar";
import { Canvas, HOP_COLORS, HOP_EMPHASIS, type LabelMode } from "./Canvas";
import { Facets } from "./Facets";
import { Inspector } from "./Inspector";
import { Outline } from "./Outline";
import { Patterns } from "./patterns/Patterns";
import { SourcePicker } from "./patterns/SourcePicker";
import { patternsAvailable } from "./patterns/model";
import {
  asOfInstant,
  childrenIndex,
  emptyFilters,
  expandModel,
  fromPath,
  fromTraverse,
  hasEdgeFilters,
  matchEntities,
  NODE_CAP,
  outlineOrder,
  relationsIn,
  shorten,
  visibleNodes,
  type EdgeFilters,
  type Selection,
  type WalkModel,
} from "./model";
import { COMMUNITIES_SHOWN, expandFailure, expandNotice, type ExpandNotice } from "./notices";

/**
 * Explorer — the three-pane Graph view. Bar on top (scope, omnibox, Walk, Path, trail);
 * Facets left (220 px); Canvas and Outline centre; Inspector right (300 px, own scroll).
 * It keeps the Graph view's walk — the canvas card, hover as DOM attributes, the memoised
 * picture, a gesture before every read, one history entry per walk with the selection
 * remembered in it, the trail — and adds expand in place (`facts.related` under the node,
 * the 200-node cap kept), pin and collapse, path mode (`facts.path` drawn as one
 * annotated chain, or "no path within N hops" stated), edge selection with the rows
 * behind it, and a full keyboard outline.
 *
 * Nothing reads before a gesture except a `?seed=` / `?path=` link.
 * Every read carries an AbortSignal and is dropped when its effect moves
 * on. A facet change never clears the selection.
 *
 * Patterns mode: on mount and on a namespace change the view asks `tracker.binding` once
 * (no upstream read); when the server names a tracker namespace the caller may read, the
 * Bar shows a Patterns toggle and the mode swaps the three panes for the bug board, the
 * tracker graph and their inspector (`./patterns/`). `?mode=patterns` is its history
 * entry.
 *
 * An expand's result — including one that changed nothing — is a toast (ok, 4 s, one at a
 * time), a line in the Inspector under the node it is about (a polite live region), and
 * the status line; the words come from `./notices`.
 */

/** An expand's toast stays 4 s. */
const EXPAND_TOAST_MS = 4000;

interface GraphHistoryState {
  graphSeed: string;
  graphSelected: string | null;
  /** Path mode: the two ends; null or absent for a walk. */
  graphPath?: [string, string] | null;
  /** The entry belongs to Patterns mode. */
  graphPatterns?: boolean;
}

interface PatternsQuery {
  component: string | null;
  severity: string | null;
  issue: number | null;
}

function queryFromUrl(): { seed: string; path: [string, string] | null; patterns: boolean; pq: PatternsQuery } {
  try {
    const p = new URLSearchParams(window.location.search);
    const raw = (p.get("path") || "").trim().toLowerCase();
    const m = /^(.+?)\s*(?:→|->)\s*(.+)$/.exec(raw);
    const issue = Number(p.get("issue"));
    return {
      seed: (p.get("seed") || "").trim().toLowerCase(),
      path: m ? [m[1].trim(), m[2].trim()] : null,
      patterns: p.get("mode") === "patterns",
      pq: {
        component: p.has("component") ? p.get("component") : null,
        severity: p.get("severity") || null,
        issue: Number.isInteger(issue) && issue > 0 ? issue : null,
      },
    };
  } catch {
    return { seed: "", path: null, patterns: false, pq: { component: null, severity: null, issue: null } };
  }
}

/** Patterns mode's URL keys, written into the current entry (replaceState). */
function writePatternsUrl(on: boolean, pq: PatternsQuery | null): void {
  try {
    const params = new URLSearchParams(window.location.search);
    for (const k of ["mode", "component", "severity", "issue"]) params.delete(k);
    if (on) {
      params.set("mode", "patterns");
      if (pq?.component !== null && pq?.component !== undefined) params.set("component", pq.component);
      if (pq?.severity) params.set("severity", pq.severity);
      if (pq?.issue) params.set("issue", String(pq.issue));
    }
    const qs = params.toString();
    const prev = readHistoryState(history.state);
    history.replaceState(
      {
        ...ownState(),
        ...(prev ?? { graphSeed: "", graphSelected: null, graphPath: null }),
        graphPatterns: on,
      },
      "",
      `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`,
    );
  } catch {
    /* history unavailable */
  }
}

function graphUrl(seed: string, path: [string, string] | null): string {
  const params = new URLSearchParams(window.location.search);
  if (path) {
    params.set("path", `${path[0]}->${path[1]}`);
    params.delete("seed");
  } else {
    params.delete("path");
    if (seed) params.set("seed", seed);
    else params.delete("seed");
  }
  const qs = params.toString();
  return `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`;
}

/** A walk or a path is a history entry: Back returns to the previous one. */
function pushEntry(seed: string, path: [string, string] | null): void {
  try {
    const state: GraphHistoryState = { graphSeed: seed, graphSelected: null, graphPath: path };
    history.pushState(state, "", graphUrl(seed, path));
  } catch {
    /* history unavailable */
  }
}

/** The keys other code keeps in this entry's state (the way back a jump into the Graph
 *  offers, lib/place.ts): rewriting the walk keeps them. */
function ownState(): Record<string, unknown> {
  return isRecord(history.state) ? history.state : {};
}

function rememberInEntry(seed: string, selected: string | null, path: [string, string] | null): void {
  try {
    const prev = readHistoryState(history.state);
    const state: GraphHistoryState = {
      graphSeed: seed,
      graphSelected: selected,
      graphPath: path,
      graphPatterns: prev?.graphPatterns === true,
    };
    history.replaceState({ ...ownState(), ...state }, "", graphUrl(seed, path));
  } catch {
    /* history unavailable */
  }
}

function readHistoryState(raw: unknown): GraphHistoryState | null {
  if (isRecord(raw) && "graphSeed" in raw) {
    const p: [string, string] | null =
      Array.isArray(raw.graphPath) && raw.graphPath.length === 2
        ? [String(raw.graphPath[0]), String(raw.graphPath[1])]
        : null;
    return {
      graphSeed: typeof raw.graphSeed === "string" ? raw.graphSeed : "",
      graphSelected: typeof raw.graphSelected === "string" ? raw.graphSelected : null,
      graphPath: p,
      graphPatterns: raw.graphPatterns === true,
    };
  }
  return null;
}

export function Explorer() {
  const identity = useIdentity();
  const initial = useMemo(queryFromUrl, []);
  const [taxonomy, setTaxonomy] = useState<TaxonomyResponse | null>(null);
  const [seed, setSeed] = useState<string>(initial.seed);
  const [pathQuery, setPathQuery] = useState<[string, string] | null>(initial.path);
  const [draft, setDraft] = useState<string>(initial.path ? `${initial.path[0]} → ${initial.path[1]}` : initial.seed);
  const [hops, setHops] = useState(2);
  const [perHop, setPerHop] = useState(12);
  const [typed, setTyped] = useState(false);
  const [labels, setLabels] = useState<LabelMode>("key");
  const [asOf, setAsOf] = useState("");
  const [filters, setFilters] = useState<EdgeFilters>(emptyFilters);
  const [memoryTypes, setMemoryTypes] = useState<Set<string>>(new Set());
  const [tags, setTags] = useState<Set<string>>(new Set());
  const [model, setModel] = useState<WalkModel | null>(null);
  const [pathRes, setPathRes] = useState<PathResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection>(null);
  const [pinned, setPinned] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [expanding, setExpanding] = useState<string | null>(null);
  const [notice, setNotice] = useState<ExpandNotice | null>(null);
  const [trail, setTrail] = useState<string[]>([]);
  const [inspectKey, setInspectKey] = useState<string | null>(null);
  const [walkedAs, setWalkedAs] = useState<string | null>(null);
  // The QA binding decides whether Patterns mode exists.
  const [binding, setBinding] = useState<TrackerBinding | null>(null);
  const [patterns, setPatterns] = useState<boolean>(initial.patterns);
  const patternsQuery = useRef<PatternsQuery>(initial.pq);
  const expandCtl = useRef<AbortController | null>(null);
  // Chips ride the NEXT walk (relations[] / labeled); changing one does not
  // re-walk, so the walk reads them through a ref.
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  const mode: "walk" | "path" = pathQuery ? "path" : "walk";

  // The taxonomy is context, not a walk: starting points, omnibox suggestions,
  // communities, types and tags.
  useAbortableEffect(
    async (signal, live) => {
      setTaxonomy(null);
      try {
        const t = await getTaxonomy(
          { org: identity.org, namespace: identity.namespace },
          { top_n_entities: 500, top_n_communities: COMMUNITIES_SHOWN, signal },
        );
        if (live())
          setTaxonomy({
            ...t,
            entities: t.entities || [],
            communities: t.communities || [],
            tags: t.tags || [],
            memory_types: t.memory_types || [],
          });
      } catch {
        if (live())
          setTaxonomy({
            tags: [],
            entities: [],
            memory_types: [],
            memory_count: 0,
            communities: [],
            communities_computed_at: "",
          });
      }
    },
    [identity.org, identity.namespace],
  );

  // Whether the namespace has a tracker plane to ask: one read per namespace, no upstream
  // read; unbound, refused and unknown are one answer and hide the mode.
  useAbortableEffect(
    async (signal, live) => {
      setBinding(null);
      try {
        const b = await trackerBinding({ org: identity.org, namespace: identity.namespace }, { signal });
        if (live()) setBinding(b);
      } catch {
        if (live())
          setBinding({ tracker_namespace: "", restricted: false, kind: "none", authorized: false, templates: [] });
      }
    },
    [identity.org, identity.namespace],
  );
  const hasPatterns = patternsAvailable(binding);
  // A namespace without a binding keeps Patterns mode when the URL or the person asked
  // for it — the pane becomes the QA-source picker (`SourcePicker`) instead of the mode
  // silently dropping to the walk.
  const needsSource = patterns && binding !== null && !hasPatterns;

  // The entry the page opened on remembers its query; Back/Forward restore
  // seed or path and the selection.
  useEffect(() => {
    if (!readHistoryState(history.state)) rememberInEntry(initial.seed, null, initial.path);
    const onPop = (e: PopStateEvent) => {
      const s = readHistoryState(e.state);
      if (!s) return;
      setPathQuery(s.graphPath ?? null);
      setSeed(s.graphSeed);
      setDraft(s.graphPath ? `${s.graphPath[0]} → ${s.graphPath[1]}` : s.graphSeed);
      setSelection(s.graphSelected ? { kind: "node", entity: s.graphSelected } : null);
      const q = queryFromUrl();
      patternsQuery.current = q.pq;
      setPatterns(Boolean(s.graphPatterns) || q.patterns);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [initial]);
  useEffect(() => {
    rememberInEntry(seed, selection?.kind === "node" ? selection.entity : null, pathQuery);
  }, [seed, selection, pathQuery]);

  // Entering or leaving Patterns mode is a history entry, like a walk.
  const togglePatterns = useCallback(
    (on: boolean) => {
      if (on === patterns) return;
      try {
        const prev = readHistoryState(history.state) ?? { graphSeed: seed, graphSelected: null, graphPath: pathQuery };
        history.pushState({ ...prev, graphPatterns: on }, "", window.location.href);
      } catch {
        /* history unavailable */
      }
      setPatterns(on);
      writePatternsUrl(on, on ? patternsQuery.current : null);
    },
    [patterns, seed, pathQuery],
  );
  const onPatternsUrl = useCallback((pq: PatternsQuery) => {
    patternsQuery.current = pq;
    writePatternsUrl(true, pq);
  }, []);

  // An expand belongs to the walk it was started on: a new walk, a new path or leaving
  // the view drops it before its result can land in another model.
  const cancelExpand = useCallback(() => {
    expandCtl.current?.abort();
    expandCtl.current = null;
    setExpanding(null);
  }, []);
  useEffect(() => cancelExpand, [cancelExpand]);
  // A walk or a path replaces the model only through here, so an expand started on the
  // old model (even one clicked while the walk was loading) never lands on the new one.
  const replaceModel = useCallback(
    (m: WalkModel | null) => {
      cancelExpand();
      setModel(m);
    },
    [cancelExpand],
  );

  const resetViewState = useCallback(() => {
    setPinned(new Set());
    setCollapsed(new Set());
    setNotice(null);
    cancelExpand();
  }, [cancelExpand]);

  // The walk. Settings re-walk and keep the selection when the entity is
  // still in the result; a new seed drops it.
  useAbortableEffect(
    async (signal, live) => {
      cancelExpand();
      if (mode !== "walk") return;
      if (!seed) {
        replaceModel(null);
        setWalkedAs(null);
        return;
      }
      setLoading(true);
      setError(null);
      const f = filtersRef.current;
      try {
        const r = await traverseFacts({ org: identity.org, namespace: identity.namespace }, seed, {
          max_hops: hops,
          top_k_per_hop: perHop,
          labeled: typed || hasEdgeFilters(f),
          as_of: asOfInstant(asOf),
          max_nodes: NODE_CAP,
          relations: f.relations.size ? [...f.relations] : undefined,
          signal,
        });
        if (!live()) return;
        const m = fromTraverse(r);
        replaceModel(m);
        setWalkedAs(r.seed !== seed ? r.seed : null);
        setSelection((prev) =>
          prev &&
          ((prev.kind === "node" && (m.nodes.has(prev.entity) || prev.entity === m.seed)) ||
            (prev.kind === "edge" && m.nodes.has(prev.child)))
            ? prev
            : null,
        );
        setTrail((t) => (t[t.length - 1] === r.seed ? t : [...t, r.seed].slice(-12)));
        setPinned((p) => new Set([...p].filter((e) => m.nodes.has(e))));
        setCollapsed((c) => new Set([...c].filter((e) => m.nodes.has(e) || e === m.seed)));
      } catch (e) {
        if (!live()) return;
        setError(humanizeError(e, { read: true }));
        replaceModel(null);
      } finally {
        if (live()) setLoading(false);
      }
    },
    [mode, seed, hops, perHop, typed, asOf, identity.org, identity.namespace],
  );

  // Path mode: one bounded search, drawn as a chain or stated as none.
  useAbortableEffect(
    async (signal, live) => {
      cancelExpand();
      if (mode !== "path" || !pathQuery) {
        setPathRes(null);
        return;
      }
      setLoading(true);
      setError(null);
      const f = filtersRef.current;
      try {
        const r = await pathFacts({ org: identity.org, namespace: identity.namespace }, pathQuery[0], pathQuery[1], {
          max_hops: Math.max(hops, 3),
          top_k_per_hop: perHop,
          labeled: typed || hasEdgeFilters(f),
          as_of: asOfInstant(asOf),
          relations: f.relations.size ? [...f.relations] : undefined,
          signal,
        });
        if (!live()) return;
        setPathRes(r);
        const m = fromPath(r);
        replaceModel(m);
        setWalkedAs(null);
        setSelection((prev) =>
          prev &&
          m &&
          ((prev.kind === "node" && (m.nodes.has(prev.entity) || prev.entity === m.seed)) ||
            (prev.kind === "edge" && m.nodes.has(prev.child)))
            ? prev
            : null,
        );
      } catch (e) {
        if (!live()) return;
        setError(humanizeError(e, { read: true }));
        replaceModel(null);
        setPathRes(null);
      } finally {
        if (live()) setLoading(false);
      }
    },
    [mode, pathQuery?.[0], pathQuery?.[1], hops, perHop, typed, asOf, identity.org, identity.namespace],
  );

  const walkFrom = useCallback(
    (entity: string) => {
      const next = entity.trim().toLowerCase();
      if (!next) return;
      if (mode === "walk" && next === seed && !patterns) return;
      if (patterns) {
        setPatterns(false);
        writePatternsUrl(false, null);
      }
      pushEntry(next, null);
      resetViewState();
      setPathQuery(null);
      setDraft(next);
      setSelection(null);
      setSeed(next);
    },
    [mode, seed, patterns, resetViewState],
  );

  const pathBetween = useCallback(
    (a: string, b: string) => {
      const pa = a.trim().toLowerCase();
      const pb = b.trim().toLowerCase();
      if (!pa || !pb || pa === pb) return;
      if (patterns) {
        setPatterns(false);
        writePatternsUrl(false, null);
      }
      pushEntry("", [pa, pb]);
      resetViewState();
      setSelection(null);
      setDraft(`${pa} → ${pb}`);
      setPathQuery([pa, pb]);
    },
    [patterns, resetViewState],
  );

  const pathToHere = useCallback(
    (entity: string) => {
      if (model) pathBetween(model.seed, entity);
    },
    [model, pathBetween],
  );

  // Expand in place: one hop under the node, the cap kept. The result lands where the
  // reader looks: a toast, the inspector line and the status line, from one derivation.
  const announce = useCallback((n: ExpandNotice) => {
    setNotice(n);
    toast(n.toast, n.tone, { key: "explorer-expand", durationMs: n.tone === "ok" ? EXPAND_TOAST_MS : undefined });
  }, []);
  const expand = useCallback(
    (entity: string) => {
      if (!model || expanding) return;
      expandCtl.current?.abort();
      const ctl = new AbortController();
      expandCtl.current = ctl;
      setExpanding(entity);
      setNotice(null);
      relatedFacts({ org: identity.org, namespace: identity.namespace }, entity, {
        top_k: perHop,
        as_of: asOfInstant(asOf),
        signal: ctl.signal,
      })
        .then((r) => {
          if (ctl.signal.aborted) return;
          // `model` is still the one this expand started on: `replaceModel` aborts `ctl`
          // before a walk or a path lands a new one.
          const out = expandModel(model, entity, r.related || [], perHop);
          setModel(out.model);
          announce(expandNotice({ entity, added: out.added.length, known: out.known, capped: out.capped }));
          setCollapsed((c) => {
            if (!c.has(entity)) return c;
            const n = new Set(c);
            n.delete(entity);
            return n;
          });
        })
        .catch((e) => {
          if (!ctl.signal.aborted) announce(expandFailure(entity, humanizeError(e, { bare: true, read: true })));
        })
        .finally(() => {
          if (!ctl.signal.aborted) setExpanding(null);
        });
    },
    [model, expanding, identity.org, identity.namespace, perHop, asOf, announce],
  );

  const togglePin = useCallback((entity: string) => {
    setPinned((p) => {
      const n = new Set(p);
      if (n.has(entity)) n.delete(entity);
      else n.add(entity);
      return n;
    });
  }, []);
  const setCollapse = useCallback((entity: string, on: boolean) => {
    setCollapsed((c) => {
      const n = new Set(c);
      if (on) n.add(entity);
      else n.delete(entity);
      return n;
    });
  }, []);

  // ---- Derived picture.
  const visible = useMemo(
    () => (model ? visibleNodes(model, filters, collapsed, pinned) : null),
    [model, filters, collapsed, pinned],
  );
  const children = useMemo(() => (visible ? childrenIndex(visible.nodes) : new Map()), [visible]);
  const order = useMemo(() => (model && visible ? outlineOrder(model.seed, children) : []), [model, visible, children]);
  const relations = useMemo(() => relationsIn(model), [model]);
  const communities = taxonomy?.communities || [];
  const hasCommunities = communities.length > 0;
  const communityLabel = useCallback(
    (cid: number | null) =>
      cid === null ? "unclustered" : (communities.find((c) => c.community_id === cid)?.label ?? `community ${cid}`),
    [communities],
  );
  const hopCounts = useMemo(() => {
    const m = new Map<number, number>();
    for (const n of visible?.nodes || []) m.set(n.hop, (m.get(n.hop) || 0) + 1);
    return [...m.entries()].sort((a, b) => a[0] - b[0]);
  }, [visible]);
  const suggestions = useMemo(() => {
    if (!taxonomy) return [];
    const walked = model?.seed ?? "";
    return matchEntities(
      taxonomy.entities.filter((e) => e.value !== walked),
      seed.trim().toLowerCase(),
      5,
    );
  }, [seed, taxonomy, model?.seed]);
  const hasChildren = useCallback(
    (entity: string) => (children.get(entity) || []).length > 0 || Boolean(visible?.hiddenUnder.get(entity)),
    [children, visible],
  );
  const inspectIdentity = useMemo<Identity>(() => ({ ...identity, agent: "*" }), [identity]);
  const entityValues = useMemo(() => new Set((taxonomy?.entities || []).map((e) => e.value)), [taxonomy]);
  const isEntity = useCallback((v: string) => entityValues.has(v.toLowerCase()), [entityValues]);
  const inPatterns = patterns && hasPatterns && binding !== null;
  // Patterns asked for, binding not read yet: neither layout, so the walk never flashes.
  const bindingPending = patterns && binding === null;

  // Nodes the server reported but did not walk through (the hub policy): the reader
  // should know a branch ends by policy, not by graph.
  const hubsHeld = useMemo(() => {
    let n = 0;
    for (const x of model?.nodes.values() ?? []) if (x.hub) n += 1;
    return n;
  }, [model]);
  const hubsNote = hubsHeld ? `; ${hubsHeld} hub${hubsHeld === 1 ? "" : "s"} held` : "";

  const summary =
    model && visible
      ? mode === "path" && pathRes
        ? `Path from ${pathRes.entity_a} to ${pathRes.entity_b}: ${pathRes.hops} hop${pathRes.hops === 1 ? "" : "s"}${pathRes.truncated ? " (search trimmed on the way)" : ""}.`
        : `${visible.nodes.length} entit${visible.nodes.length === 1 ? "y" : "ies"} within ${hops} hop${hops === 1 ? "" : "s"} of ${model.seed}${hopCounts.map(([h, c]) => `; hop ${h}: ${c}`).join("")}${model.truncated ? `; strongest ${NODE_CAP} kept` : ""}${hubsNote}.`
      : "";

  return (
    <div className="lg:h-full min-h-0 flex flex-col px-6 py-5 gap-3">
      {/* A jump in from something being read (an entity on a memory) offers the way back,
          above the toolbar; a walk taken here is a new entry without one. */}
      <BackLink />
      <Bar
        identity={identity}
        entities={taxonomy ? taxonomy.entities : null}
        draft={draft}
        onDraft={setDraft}
        onWalk={walkFrom}
        onPath={pathBetween}
        trail={trail}
        currentSeed={mode === "walk" ? (model ? model.seed : seed) || null : null}
        busy={loading}
        patternsAvailable={hasPatterns || patterns}
        patterns={inPatterns || needsSource}
        onPatterns={togglePatterns}
      />

      {bindingPending && <Loading label="Loading patterns" />}
      {needsSource && <SourcePicker org={identity.org} current={identity.namespace} />}

      {inPatterns && binding && (
        <Patterns
          key={`${identity.org}/${identity.namespace}`}
          identity={identity}
          binding={binding}
          isEntity={isEntity}
          onWalk={walkFrom}
          initial={patternsQuery.current}
          onUrlState={onPatternsUrl}
        />
      )}

      {/* Fixed footprints: 220 px facets, the canvas column, 300 px inspector;
            the single grid row is bounded by the page so each pane owns its scroll. */}
      {!inPatterns && !needsSource && !bindingPending && (
        <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[220px_minmax(0,1fr)_300px] lg:grid-rows-[minmax(0,1fr)] gap-4">
          <Facets
            taxonomy={taxonomy}
            seed={model?.seed ?? null}
            relations={relations}
            filters={filters}
            onFilters={setFilters}
            filteredOut={visible?.filteredOut ?? 0}
            memoryTypes={memoryTypes}
            onMemoryTypes={setMemoryTypes}
            tags={tags}
            onTags={setTags}
            asOf={asOf}
            onAsOf={setAsOf}
            hops={hops}
            onHops={setHops}
            perHop={perHop}
            onPerHop={setPerHop}
            typed={typed}
            onTyped={setTyped}
            labels={labels}
            onLabels={setLabels}
            onWalk={walkFrom}
            patternsNote={binding && binding.kind === "none" ? "Patterns are available on QA-bound namespaces." : null}
          />

          <div className="min-w-0 min-h-0 flex flex-col">
            {/* Summary line: fixed height so a notice never moves the canvas. */}
            <div
              className="h-6 mb-2 text-xs text-text-muted flex items-center gap-2 overflow-hidden whitespace-nowrap"
              aria-live="polite"
            >
              {model && visible && !loading && !error && (
                <>
                  <span className="truncate">
                    {mode === "path" && pathRes ? (
                      <>
                        path <span className="font-mono text-text">{pathRes.entity_a}</span> →{" "}
                        <span className="font-mono text-text">{pathRes.entity_b}</span>:{" "}
                        <span className="tabular-nums text-text">{pathRes.hops}</span> hop
                        {pathRes.hops === 1 ? "" : "s"}
                        {pathRes.truncated ? " · search trimmed" : ""}
                      </>
                    ) : (
                      <>
                        <span className="tabular-nums text-text">{visible.nodes.length}</span> entit
                        {visible.nodes.length === 1 ? "y" : "ies"} within {hops} hop{hops === 1 ? "" : "s"} of{" "}
                        <span className="font-mono text-text">{model.seed}</span>
                        {hopCounts.map(([h, c]) => (
                          <span key={h} className="ml-2 inline-flex items-center gap-1">
                            <span
                              className="inline-block w-2 h-2 rounded-full"
                              style={{
                                background: HOP_COLORS[Math.min(h, HOP_COLORS.length - 1)],
                                opacity: hasCommunities ? 1 : HOP_EMPHASIS[Math.min(h, HOP_EMPHASIS.length - 1)],
                              }}
                              aria-hidden="true"
                            />
                            hop {h}: <span className="tabular-nums">{c}</span>
                          </span>
                        ))}
                        {model.truncated ? ` · strongest ${NODE_CAP} kept` : ""}
                        {hubsHeld ? ` · ${hubsHeld} hub${hubsHeld === 1 ? "" : "s"} held` : ""}
                        {asOf ? ` · as of ${asOf}` : ""}
                      </>
                    )}
                  </span>
                  {walkedAs && (
                    <Badge
                      className="shrink-0"
                      state="neutral"
                      size="sm"
                      text={
                        <>
                          typed <span className="font-mono">{shorten(seed, 16)}</span>, walked as{" "}
                          <span className="font-mono">{shorten(walkedAs, 16)}</span>
                        </>
                      }
                    />
                  )}
                  {suggestions.length > 0 && (walkedAs || model.nodes.size === 0) && (
                    <span className="shrink-0 inline-flex items-center gap-1">
                      Other matches:
                      {suggestions.map((e) => (
                        <Link
                          key={e.value}
                          component="button"
                          type="button"
                          size="sm"
                          weight="mono"
                          onClick={() => walkFrom(e.value)}
                        >
                          {shorten(e.value, 16)}
                        </Link>
                      ))}
                    </span>
                  )}
                  {notice && <span className="shrink-0 text-text">· {notice.toast}</span>}
                </>
              )}
              {mode === "path" && pathRes && !pathRes.found && !loading && (
                <span className="truncate">
                  no path <span className="font-mono">{pathRes.entity_a}</span> →{" "}
                  <span className="font-mono">{pathRes.entity_b}</span> within {pathRes.hops} hop
                  {pathRes.hops === 1 ? "" : "s"}
                </span>
              )}
            </div>
            <Canvas
              model={model}
              visible={visible}
              mode={mode}
              pathRes={pathRes}
              loading={loading}
              loadingLabel={
                mode === "path" && pathQuery
                  ? `Loading the path from ${pathQuery[0]} to ${pathQuery[1]}`
                  : `Loading the walk from ${seed}`
              }
              error={error}
              seedTyped={seed}
              selection={selection}
              pinned={pinned}
              labels={labels}
              typed={typed}
              asOf={asOf}
              hasCommunities={hasCommunities}
              communityLabel={communityLabel}
              starters={taxonomy ? taxonomy.entities.slice(0, 12) : null}
              taxonomyEmpty={Boolean(taxonomy && taxonomy.entities.length === 0)}
              onWalk={walkFrom}
              onSelect={setSelection}
              onExpand={expand}
              onUncollapse={(e) => setCollapse(e, false)}
            />
            {model && visible && !loading && !error && visible.nodes.length > 0 && (
              <Outline
                model={model}
                visible={visible}
                children={children}
                order={order}
                collapsed={collapsed}
                pinned={pinned}
                selection={selection}
                typed={typed}
                hasCommunities={hasCommunities}
                communityLabel={communityLabel}
                summary={summary}
                expanding={expanding}
                onSelect={setSelection}
                onWalk={walkFrom}
                onPath={pathToHere}
                onExpand={expand}
                onCollapse={setCollapse}
                onPin={togglePin}
              />
            )}
          </div>

          <Inspector
            identity={identity}
            model={model}
            selection={selection}
            taxonomy={taxonomy}
            hasCommunities={hasCommunities}
            communityLabel={communityLabel}
            asOf={asOf}
            typed={typed}
            hops={hops}
            provenance={filters.provenance}
            memoryTypes={memoryTypes}
            tags={tags}
            pinned={pinned}
            collapsed={collapsed}
            hasChildren={hasChildren}
            expanding={expanding}
            notice={notice}
            onWalk={walkFrom}
            onPath={pathToHere}
            onSelect={setSelection}
            onExpand={expand}
            onPin={togglePin}
            onCollapse={setCollapse}
            onOpenMemory={setInspectKey}
          />
        </div>
      )}

      {inspectKey && (
        <MemoryInspector
          // Search hits and fact sources carry no writer agent; "*" reads the
          // row whichever agent wrote it (memory.get treats it as namespace-wide).
          identity={inspectIdentity}
          memoryKey={inspectKey}
          onClose={() => setInspectKey(null)}
          onChanged={() => {
            /* the graph is a projection of the rows; a re-walk picks changes up */
          }}
          // Already on the Graph: an entity's "Open in Graph" walks from it here.
          onWalkEntity={(entity) => {
            setInspectKey(null);
            walkFrom(entity);
          }}
          // Another memory opens in this drawer, not in a second one over it.
          onInspectMemory={setInspectKey}
        />
      )}
    </div>
  );
}
