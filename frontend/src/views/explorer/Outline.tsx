import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { IconButton } from "../../components/IconButton";
import { DirectionIcon, directionWord } from "./Direction";
import { relationLabel, shorten, type Selection, type VisibleTree, type WalkModel, type XNode } from "./model";

/**
 * Outline — the walk as a tree a keyboard can read. One row per drawn node in tree order,
 * the seed as the root; each row is one line of name and one of metadata (hop, the edge
 * that reached it, the community when known). Roving tabindex: Up / Down move, Right expands
 * a collapsed row or moves into its children, Left collapses or moves to the parent, Home /
 * End jump to the ends, Enter or Space selects. A click on the row selects its node; the
 * edge that reached it has its own button at the row's end, clear of the name and the
 * metadata. The other actions (expand in place, pin, walk from here, path from the seed)
 * are drawn on the selected row only, so 200 rows stay 200 rows; the selected row's
 * buttons, its edge button included, are its tab stops. A tree of more than 20 rows shows
 * the first 20 and "Show all N", unless the selection sits further down.
 */

/** Rows shown before "Show all N". */
const CAP = 20;

export interface OutlineProps {
  model: WalkModel;
  visible: VisibleTree;
  children: Map<string, XNode[]>;
  order: XNode[];
  collapsed: Set<string>;
  pinned: Set<string>;
  selection: Selection;
  typed: boolean;
  hasCommunities: boolean;
  communityLabel: (cid: number | null) => string;
  summary: string;
  expanding: string | null;
  onSelect: (sel: Selection) => void;
  onWalk: (entity: string) => void;
  onPath: (entity: string) => void;
  onExpand: (entity: string) => void;
  onCollapse: (entity: string, collapsed: boolean) => void;
  onPin: (entity: string) => void;
}

export function Outline({
  model,
  visible,
  children,
  order,
  collapsed,
  pinned,
  selection,
  typed,
  hasCommunities,
  communityLabel,
  summary,
  expanding,
  onSelect,
  onWalk,
  onPath,
  onExpand,
  onCollapse,
  onPin,
}: OutlineProps) {
  const rows: XNode[] = [
    {
      entity: model.seed,
      hop: 0,
      via_co_occur: 0,
      via_relation: "",
      via_direction: "",
      via_provenance: "",
      valid_from: "",
      valid_to: "",
      community_id: model.seedCommunityId,
      path: [model.seed],
      parent: null,
      origin: "walk",
    },
    ...order,
  ];
  const [active, setActive] = useState<string>(model.seed);
  const [all, setAll] = useState(false);
  const refs = useRef(new Map<string, HTMLDivElement>());
  // A new walk starts capped again.
  useEffect(() => setAll(false), [model.seed]);
  // A new picture puts the roving focus back on the seed when the active
  // row is gone.
  useEffect(() => {
    if (!rows.some((r) => r.entity === active)) setActive(model.seed);
    // Only a new picture resets focus; a roving `active` change must not (`rows` derives from props).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model, visible]);

  // A row past the cap is drawn once it is the active one; focus waits for it to be.
  const pendingFocus = useRef<string | null>(null);
  const focusRow = useCallback((entity: string) => {
    setActive(entity);
    const row = refs.current.get(entity);
    if (row) row.focus();
    else pendingFocus.current = entity;
  }, []);
  useEffect(() => {
    const want = pendingFocus.current;
    if (want === null) return;
    pendingFocus.current = null;
    refs.current.get(want)?.focus();
  });

  const selectedEntity = selection?.kind === "node" ? selection.entity : null;
  const selectedEdgeChild = selection?.kind === "edge" ? selection.child : null;
  const selectedAt = rows.findIndex((r) => r.entity === selectedEntity || r.entity === selectedEdgeChild);
  // The cap never hides the selected row or the row holding the tree's one tab stop; once
  // either lands past it, the whole tree stays drawn for this walk.
  const activeAt = rows.findIndex((r) => r.entity === active);
  if (!all && (selectedAt >= CAP || activeAt >= CAP)) setAll(true);
  const capped = !all && rows.length > CAP && selectedAt < CAP && activeAt < CAP;
  const shown = capped ? rows.slice(0, CAP) : rows;

  const onKey = (e: ReactKeyboardEvent<HTMLDivElement>, n: XNode, index: number) => {
    const kids = children.get(n.entity) || [];
    const hasKids = kids.length > 0 || (visible.hiddenUnder.get(n.entity) || 0) > 0;
    const isCollapsed = collapsed.has(n.entity);
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        if (index + 1 < shown.length) focusRow(shown[index + 1].entity);
        break;
      case "ArrowUp":
        e.preventDefault();
        if (index > 0) focusRow(shown[index - 1].entity);
        break;
      case "Home":
        e.preventDefault();
        focusRow(shown[0].entity);
        break;
      case "End":
        e.preventDefault();
        focusRow(shown[shown.length - 1].entity);
        break;
      case "ArrowRight":
        e.preventDefault();
        if (isCollapsed) onCollapse(n.entity, false);
        else if (kids.length) focusRow(kids[0].entity);
        break;
      case "ArrowLeft":
        e.preventDefault();
        if (hasKids && !isCollapsed) onCollapse(n.entity, true);
        else if (n.parent) focusRow(n.parent);
        break;
      case "Enter":
      case " ":
        // A button in the row (its actions, the edge) activates itself.
        if (e.target !== e.currentTarget) return;
        e.preventDefault();
        onSelect(selectedEntity === n.entity ? null : { kind: "node", entity: n.entity });
        break;
      default:
        return;
    }
  };

  return (
    <div className="explorer-outline mt-3 lg:flex-1 lg:min-h-0 flex flex-col">
      <p className="text-xs text-text-muted mb-1" id="explorer-outline-summary">
        {summary}
      </p>
      <div
        role="tree"
        aria-label="Walk outline"
        aria-describedby="explorer-outline-summary"
        className="lg:flex-1 min-h-[120px] max-h-[320px] lg:max-h-none overflow-auto rounded-lg border border-border bg-surface text-xs"
      >
        {shown.map((n, i) => {
          const kids = children.get(n.entity) || [];
          const hidden = visible.hiddenUnder.get(n.entity) || 0;
          const hasKids = kids.length > 0 || hidden > 0;
          const isCollapsed = collapsed.has(n.entity);
          const isSelected = selectedEntity === n.entity;
          const edgeSelected = selectedEdgeChild === n.entity;
          const parent = n.parent;
          const isSeed = n.entity === model.seed;
          const typedEdge = typed && !!n.via_relation;
          const relation = typedEdge
            ? `${relationLabel(n.via_relation)} (${directionWord(n.via_direction)})`
            : `co-mentioned ${n.via_co_occur}×`;
          return (
            <div
              key={n.entity}
              ref={(el) => {
                if (el) refs.current.set(n.entity, el);
                else refs.current.delete(n.entity);
              }}
              role="treeitem"
              aria-level={n.hop + 1}
              aria-selected={isSelected}
              data-edge-selected={edgeSelected || undefined}
              aria-expanded={hasKids ? !isCollapsed : undefined}
              aria-label={`${n.entity} · hop ${n.hop}${n.parent ? ` · ${relation}` : ""}${pinned.has(n.entity) ? " · pinned" : ""}${hidden ? ` · ${hidden} hidden` : ""}`}
              tabIndex={active === n.entity ? 0 : -1}
              onFocus={() => setActive(n.entity)}
              onKeyDown={(e) => onKey(e, n, i)}
              onClick={(e) => {
                if (e.target instanceof Element && e.target.closest("button")) return;
                onSelect(isSelected ? null : { kind: "node", entity: n.entity });
              }}
              className={`group flex items-start gap-1.5 px-2 py-1 border-b border-border last:border-b-0 cursor-default`}
              style={{ paddingLeft: `${8 + n.hop * 14}px` }}
            >
              <button
                type="button"
                tabIndex={-1}
                aria-hidden="true"
                disabled={!hasKids}
                onClick={() => onCollapse(n.entity, !isCollapsed)}
                className={`mt-0.5 shrink-0 w-4 h-4 grid place-items-center rounded text-text-muted ${hasKids ? "hover:text-text" : "opacity-0"}`}
              >
                <Icon name={isCollapsed ? "chevron-right" : "chevron-down"} size="xs" aria-hidden="true" />
              </button>
              <div className="min-w-0 flex-1">
                {/* Line 1, the primary: the entity at 14 px. */}
                <div className="flex items-center gap-1 min-w-0">
                  {isSeed ? <span className="text-xs text-text-muted shrink-0">seed</span> : null}
                  <span className={`truncate text-sm text-text ${isSelected ? "font-semibold" : ""}`}>{n.entity}</span>
                  {pinned.has(n.entity) && (
                    <Icon name="pin" size="xs" aria-hidden="true" className="shrink-0" color="var(--color-accent)" />
                  )}
                  {hidden > 0 && (
                    <Badge text={`+${hidden}`} state="neutral" size="sm" className="inline-flex shrink-0" />
                  )}
                </div>
                {/* Line 2, muted 12 px: "hop 1 · co-mentioned 20× with logical". */}
                <div className="text-xs text-text-muted flex items-center gap-1.5 min-w-0">
                  {parent ? (
                    <span className={`truncate ${edgeSelected ? "text-text" : ""}`}>
                      hop {n.hop} ·{" "}
                      {typedEdge ? (
                        <>
                          <DirectionIcon direction={n.via_direction} />
                          {relationLabel(n.via_relation)}
                        </>
                      ) : (
                        relation
                      )}{" "}
                      with {shorten(parent, 20)}
                    </span>
                  ) : (
                    <span className="tabular-nums shrink-0">hop {n.hop}</span>
                  )}
                  {hasCommunities && n.community_id !== null && (
                    <span className="truncate">· {communityLabel(n.community_id)}</span>
                  )}
                  {n.origin === "expand" && <span className="text-text-muted">· expanded</span>}
                </div>
              </div>
              {isSelected && (
                <div className="flex items-center shrink-0">
                  <IconButton
                    label={isCollapsed ? "Show hidden children" : `Expand ${shorten(n.entity, 24)} in place`}
                    onClick={() => (isCollapsed ? onCollapse(n.entity, false) : onExpand(n.entity))}
                    disabled={expanding === n.entity}
                    icon="plus"
                    size="sm"
                  />
                  {!isSeed && (
                    <IconButton
                      label={pinned.has(n.entity) ? "Unpin" : "Pin (survives facet changes)"}
                      onClick={() => onPin(n.entity)}
                      icon={pinned.has(n.entity) ? "unpin" : "pin"}
                      size="sm"
                    />
                  )}
                  {!isSeed && (
                    <IconButton label="Walk from here" onClick={() => onWalk(n.entity)} icon="arrow-right" size="sm" />
                  )}
                  {!isSeed && (
                    <IconButton
                      label="Path from the seed to here"
                      onClick={() => onPath(n.entity)}
                      icon="arrow-directions"
                      size="sm"
                    />
                  )}
                </div>
              )}
              {parent && (
                <IconButton
                  label={`Select the edge: ${relation} with ${parent}`}
                  onClick={() => onSelect(edgeSelected ? null : { kind: "edge", parent, child: n.entity })}
                  icon="share-network"
                  size="sm"
                  // The selected row's actions are its tab stops; the other rows' are reached by selecting them.
                  tabIndex={isSelected ? undefined : -1}
                  aria-pressed={edgeSelected}
                  className="shrink-0"
                />
              )}
            </div>
          );
        })}
      </div>
      {capped && (
        <div className="mt-1">
          <Button
            type="empty"
            htmlType="button"
            onClick={() => {
              // The button goes with the cap: focus moves to the first row it hid.
              const next = rows[CAP];
              setAll(true);
              if (next) focusRow(next.entity);
            }}
          >
            Show all {rows.length}
          </Button>
        </div>
      )}
    </div>
  );
}
