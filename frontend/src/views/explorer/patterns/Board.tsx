import { useCallback, useEffect, useRef, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { EmptyState } from "../../../components/EmptyState";
import { Skeleton } from "../../../components/Skeleton";
import { LoadingSkeleton, Tip } from "../../../components/ui";
import { heat, pct, trendGlyph, type Board as BoardModel, type BoardRow } from "./model";

/**
 * Board — the component × severity heat table. One `grid` with roving focus: the arrow
 * keys move between cells, Home/End along a row, PageUp/PageDown between rows' first
 * cells, ↵ or Space selects the focused cell (or clears it when it is selected). A cell
 * shows its open count and, smaller, the untested share; its heat is one hue mixed into
 * the surface by `color-mix` so the tail of ones and twos stays legible against a 236.
 * The row header carries the component, its open total, and — when the `test_gaps`
 * snapshot names it — the gap ratio and a trend arrow, with a tooltip that spells the
 * numbers out.
 *
 * The stated results (empty, `tracker_unavailable`, `restricted_required`, an
 * HTTP error) render in the same box as an `EmptyState`, never a blank.
 */

export type BoardStatus =
  | "loading"
  | "served"
  | "empty"
  | "tracker_unavailable"
  | "restricted_required"
  | "error"
  | "unplanned";

export interface BoardProps {
  board: BoardModel | null;
  status: BoardStatus;
  error?: string | null;
  /** Rows before the facets narrowed them (for the "N of M" caption). */
  totalRows?: number;
  selected: { component: string; severity: string } | null;
  onSelect: (cell: { component: string; severity: string } | null) => void;
  trackerNamespace: string;
  daysLabel: string;
}

const HEAT_STYLE = (share: number): CSSProperties => ({
  // One hue, one token: the cell's ground is `--color-warn` mixed into the
  // surface by the cell's share of the largest count (8–72 %).
  background:
    share > 0
      ? `color-mix(in oklab, var(--color-warn) ${Math.round(8 + share * 64)}%, var(--color-surface))`
      : "var(--color-surface)",
});

/** The cell's DOM id: component and severity joined by a separator no
 *  component name carries (attribute values cannot hold a NUL). */
const SEP = "::";
function cellId(component: string, severity: string): string {
  return `${component}${SEP}${severity}`;
}
function splitCellId(id: string): [string, string] {
  const i = id.lastIndexOf(SEP);
  return i < 0 ? [id, ""] : [id.slice(0, i), id.slice(i + SEP.length)];
}

// The gap ratio's trend since the previous snapshot: more bugs without a test is worse.
const TREND_ICON: Record<string, IconName> = { worse: "arrow-up", better: "arrow-down", flat: "minus" };

function RowHeader({ row }: { row: BoardRow }) {
  const g = row.gap;
  const t = g ? trendGlyph(g.trend_delta) : null;
  const tip =
    g && t
      ? `${row.label}: ${row.open} open · ${row.untested} without a linked test. Snapshot ${g.snapshot_date}: gap ratio ${pct(g.gap_ratio)} (${g.bugs_without_test} of ${g.total_bugs} bugs without a test), trend ${g.trend_delta >= 0 ? "+" : ""}${g.trend_delta.toFixed(2)} (${t.word} than the previous snapshot), risk score ${Math.round(g.risk_score)}.`
      : `${row.label}: ${row.open} open · ${row.untested} without a linked test. No risk snapshot names this component.`;
  return (
    <Tip content={tip}>
      {/* biome-ignore lint/a11y/useSemanticElements: the row header of the div data grid in Board */}
      <div
        role="rowheader"
        tabIndex={0}
        className="sticky left-0 z-10 bg-surface-2 px-2 py-1 flex items-center gap-2 min-w-0 border-r border-border text-xs"
        aria-label={`${row.label}, ${row.open} open${g && t ? `, gap ratio ${pct(g.gap_ratio)}, trend ${t.word}` : ""}`}
      >
        <span className="truncate flex-1 min-w-0">{row.label}</span>
        <span className="tabular-nums text-text-muted shrink-0">{row.open}</span>
        {g && t ? (
          <span
            className={`tabular-nums shrink-0 inline-flex items-center gap-0.5 ${t.word === "worse" ? "text-danger" : t.word === "better" ? "text-success" : "text-text-muted"}`}
            aria-hidden="true"
          >
            {pct(g.gap_ratio)} <Icon name={TREND_ICON[t.word] ?? "minus"} size="xs" aria-hidden="true" />
          </span>
        ) : (
          <span className="text-text-muted shrink-0" aria-hidden="true">
            —
          </span>
        )}
      </div>
    </Tip>
  );
}

export function Board({
  board,
  status,
  error,
  totalRows,
  selected,
  onSelect,
  trackerNamespace,
  daysLabel,
}: BoardProps) {
  const gridRef = useRef<HTMLDivElement>(null);
  const focusId = useRef<string | null>(null);

  // Roving focus: the focused cell carries tabIndex 0, the rest -1. The
  // first cell (or the selected one) is the entry point.
  const cells = board
    ? board.rows.flatMap((r) => board.severities.filter((s) => r.cells.has(s)).map((s) => ({ row: r, severity: s })))
    : [];
  const entry =
    selected && board?.rows.some((r) => r.component === selected.component && r.cells.has(selected.severity))
      ? cellId(selected.component, selected.severity)
      : cells.length
        ? cellId(cells[0].row.component, cells[0].severity)
        : null;
  if (!focusId.current || !cells.some((c) => cellId(c.row.component, c.severity) === focusId.current))
    focusId.current = entry;

  useEffect(() => {
    focusId.current = entry;
  }, [board, entry]);

  const focusCell = useCallback((id: string) => {
    const grid = gridRef.current;
    if (!grid) return;
    const prev = focusId.current;
    focusId.current = id;
    const esc = (v: string) => v.replace(/["\\]/g, "\\$&");
    if (prev) grid.querySelector<HTMLElement>(`[data-cell="${esc(prev)}"]`)?.setAttribute("tabindex", "-1");
    const el = grid.querySelector<HTMLElement>(`[data-cell="${esc(id)}"]`);
    if (el) {
      el.setAttribute("tabindex", "0");
      el.focus();
    }
  }, []);

  const onKey = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      if (!board) return;
      const target = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-cell]") : null;
      if (!target) return;
      const id = target.getAttribute("data-cell") || "";
      const [component, severity] = splitCellId(id);
      const rowIdx = board.rows.findIndex((r) => r.component === component);
      if (rowIdx < 0) return;
      const rowSevs = board.severities.filter((s) => board.rows[rowIdx].cells.has(s));
      const colIdx = rowSevs.indexOf(severity);
      const rowAt = (i: number, wantSeverity: string): string | null => {
        const r = board.rows[i];
        if (!r) return null;
        const sevs = board.severities.filter((s) => r.cells.has(s));
        if (!sevs.length) return null;
        const s = sevs.includes(wantSeverity) ? wantSeverity : (sevs[Math.min(colIdx, sevs.length - 1)] ?? sevs[0]);
        return cellId(r.component, s);
      };
      let next: string | null = null;
      switch (e.key) {
        case "ArrowRight":
          next = colIdx < rowSevs.length - 1 ? cellId(component, rowSevs[colIdx + 1]) : null;
          break;
        case "ArrowLeft":
          next = colIdx > 0 ? cellId(component, rowSevs[colIdx - 1]) : null;
          break;
        case "ArrowDown":
          next = rowAt(rowIdx + 1, severity);
          break;
        case "ArrowUp":
          next = rowAt(rowIdx - 1, severity);
          break;
        case "Home":
          next = e.ctrlKey ? rowAt(0, severity) : cellId(component, rowSevs[0]);
          break;
        case "End":
          next = e.ctrlKey ? rowAt(board.rows.length - 1, severity) : cellId(component, rowSevs[rowSevs.length - 1]);
          break;
        case "PageDown":
          next = rowAt(Math.min(board.rows.length - 1, rowIdx + 10), severity);
          break;
        case "PageUp":
          next = rowAt(Math.max(0, rowIdx - 10), severity);
          break;
        case "Enter":
        case " ":
          e.preventDefault();
          onSelect(
            selected && selected.component === component && selected.severity === severity
              ? null
              : { component, severity },
          );
          return;
        default:
          return;
      }
      if (next) {
        e.preventDefault();
        focusCell(next);
      }
    },
    [board, selected, onSelect, focusCell],
  );

  const onClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const target = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-cell]") : null;
      if (!target) return;
      const id = target.getAttribute("data-cell") || "";
      const [component, severity] = splitCellId(id);
      focusCell(id);
      onSelect(
        selected && selected.component === component && selected.severity === severity ? null : { component, severity },
      );
    },
    [selected, onSelect, focusCell],
  );

  const frame = "relative rounded-xl border border-border bg-surface overflow-hidden min-h-[320px] flex flex-col";

  // A new window keeps the board on screen, marked busy, until it answers.
  const refreshing = status === "loading";
  if (refreshing && !board) {
    return (
      <LoadingSkeleton label="Loading the bug board" className={frame}>
        <div className="p-4 space-y-2">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-7 w-full" />
          ))}
        </div>
      </LoadingSkeleton>
    );
  }
  if (status === "error") {
    return (
      <div className={`${frame} grid place-items-center p-6 text-sm text-danger`}>
        {error || "The tracker plane did not answer."}
      </div>
    );
  }
  if (status === "tracker_unavailable") {
    return (
      <div className={`${frame} grid place-items-center`}>
        <EmptyState
          icon="services"
          title="The tracker is not answering"
          description={`The source behind ${trackerNamespace} could not be read (breaker open, budget exceeded, or the credential unset). Nothing on the board is a claim; try again shortly.`}
        />
      </div>
    );
  }
  if (status === "restricted_required") {
    return (
      <div className={`${frame} grid place-items-center`}>
        <EmptyState
          icon="lock"
          title="Restricted — not available with this token"
          description="The bug board on this namespace needs a restricted tracker namespace your token does not carry."
        />
      </div>
    );
  }
  if (status === "unplanned") {
    return (
      <div className={`${frame} grid place-items-center`}>
        <EmptyState
          icon="services"
          title="No board was planned"
          description={`${trackerNamespace} answered, but its catalogue routed the board question to nothing; the server may predate the bug board.`}
        />
      </div>
    );
  }
  if (!board || board.rows.length === 0) {
    return (
      <div className={`${frame} grid place-items-center`}>
        <EmptyState
          icon="services"
          title={
            totalRows
              ? "Every cell is filtered out"
              : `No open issues in ${daysLabel === "all open" ? "any window" : `the last ${daysLabel}`}`
          }
          description={
            totalRows
              ? "The component, severity or flag chips on the left hide every cell. Clear a chip to see the board again."
              : `${trackerNamespace} counts no open issue by component and severity here.`
          }
        />
      </div>
    );
  }

  const cols = `minmax(180px,260px) repeat(${board.severities.length}, minmax(72px, 1fr))`;
  return (
    <div className={frame} aria-busy={refreshing || undefined}>
      <div className="overflow-auto overscroll-contain flex-1 min-h-0">
        {/* biome-ignore-start lint/a11y/useSemanticElements: APG data grid with a roving tabindex over the count cells; a <table> cannot hold the sticky CSS-grid layout */}
        {/* biome-ignore-start lint/a11y/useFocusableInteractive: APG data grid — only the count cells take the roving focus; headers and empty cells are read, not visited */}
        <div
          ref={gridRef}
          role="grid"
          aria-label={`Open issues by component and severity: ${board.rows.length} components, ${board.totalOpen} open`}
          aria-rowcount={board.rows.length + 1}
          aria-colcount={board.severities.length + 1}
          className="grid text-xs w-max min-w-full"
          style={{ gridTemplateColumns: cols }}
          onKeyDown={onKey}
          onClick={onClick}
        >
          <div role="row" className="contents">
            <div
              role="columnheader"
              className="sticky top-0 left-0 z-20 bg-surface-2 px-2 py-1.5 section-label border-b border-r border-border"
            >
              component · open · gap, trend
            </div>
            {board.severities.map((s) => (
              <div
                key={s}
                role="columnheader"
                className="sticky top-0 z-10 bg-surface-2 px-2 py-1.5 section-label border-b border-border text-center"
              >
                {s}
              </div>
            ))}
          </div>
          {board.rows.map((r) => (
            <div key={r.component} role="row" className="contents">
              <RowHeader row={r} />
              {board.severities.map((s) => {
                const c = r.cells.get(s);
                if (!c)
                  return (
                    <div
                      key={s}
                      role="gridcell"
                      aria-label={`${r.label} ${s}: none`}
                      className="border-b border-border/60 px-2 py-1 text-text-muted text-center"
                      aria-disabled="true"
                    >
                      ·
                    </div>
                  );
                const id = cellId(r.component, s);
                const isSel = selected?.component === r.component && selected.severity === s;
                const share = heat(c.open, board.maxOpen);
                return (
                  <div
                    key={s}
                    role="gridcell"
                    data-cell={id}
                    tabIndex={focusId.current === id ? 0 : -1}
                    aria-selected={isSel}
                    aria-label={`${r.label} ${s}: ${c.open} open, ${c.untested} untested${c.flaky ? `, ${c.flaky} flaky` : ""}${c.bisected ? `, ${c.bisected} bisected` : ""}`}
                    style={HEAT_STYLE(share)}
                    className={`board-cell border-b border-border/60 px-2 py-1 text-center cursor-pointer select-none ${isSel ? "ring-2 ring-inset ring-text" : ""}`}
                  >
                    <span className="tabular-nums font-medium">{c.open}</span>
                    <span className="tabular-nums text-text-muted ml-1 text-xs" aria-hidden="true">
                      {c.open ? Math.round((c.untested / c.open) * 100) : 0}%
                    </span>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
        {/* biome-ignore-end lint/a11y/useFocusableInteractive: the grid ends */}
        {/* biome-ignore-end lint/a11y/useSemanticElements: the grid ends */}
      </div>
      <div className="px-3 py-1.5 border-t border-border text-xs text-text-muted flex items-center gap-3 flex-wrap">
        <span>
          <span className="tabular-nums text-text">{board.rows.length}</span>
          {totalRows && totalRows !== board.rows.length ? ` of ${totalRows}` : ""} components ·{" "}
          <span className="tabular-nums text-text">{board.totalOpen}</span> open · {daysLabel}
        </span>
        <span>cell = open · untested share; row = component · open · gap ratio and trend</span>
        <span className="inline-flex items-center gap-1">
          <span
            className="inline-block w-3 h-3 rounded-sm border border-border"
            style={HEAT_STYLE(0.1)}
            aria-hidden="true"
          />
          <span
            className="inline-block w-3 h-3 rounded-sm border border-border"
            style={HEAT_STYLE(0.55)}
            aria-hidden="true"
          />
          <span
            className="inline-block w-3 h-3 rounded-sm border border-border"
            style={HEAT_STYLE(1)}
            aria-hidden="true"
          />{" "}
          more open
        </span>
      </div>
    </div>
  );
}
