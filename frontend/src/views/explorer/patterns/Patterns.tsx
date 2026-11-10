import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { TabTrigger } from "../../../components/ui";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import {
  trackerAnswer,
  trackerItem,
  type Identity,
  type TrackerAnswer,
  type TrackerBinding,
  type TrackerItem,
  type TrackerSection,
} from "../../../api";
import { useAbortableEffect } from "../../../lib/hooks";
import { humanizeError } from "../../../lib/errorWords";
import { Board, type BoardStatus } from "./Board";
import {
  buildBoard,
  cellIssues,
  duplicateFrom,
  emptyFacets,
  filterBoard,
  issuesForCell,
  neighbourNumbers,
  trackerGraphFrom,
  restrictedSections,
  WINDOWS,
  type Board as BoardModel,
  type DuplicateRow,
  type Facets,
  type Issue,
  type Window,
} from "./model";
import { PatternsFacets } from "./PatternsFacets";
import { PatternsInspector, type SimilarHit } from "./PatternsInspector";
import { TrackerGraph } from "./TrackerGraph";
import { oneOf } from "../../../lib/oneOf";

/**
 * Patterns — the Explorer's second mode: what kinds of bugs a namespace carries and where
 * they repeat. It reuses the three-pane shell (facets 220 px · centre · inspector 300 px)
 * with its own panes: PatternsFacets, the Board (or the tracker graph, a tab beside it),
 * and PatternsInspector.
 *
 * Reads, each through `tracker.answer` on the bound tracker namespace unless named:
 * - the board: "bug board" with `anchors.days` (the window) and, in the
 *   same effect, "test gaps" for the row headers' gap ratio and trend;
 * - a cell: `anchors {components: [c], severities: [s], words: ["risk"]}` —
 *   the area plan, which serves `component_risk` and `area_open_issues`
 * filtered to the cell on the server (every issue the board counted, up to 300), and
 *   `area_incidents` / `area_escalations`: rows on a restricted tracker namespace,
 *   `restricted_required` elsewhere;
 * - an issue: `tracker.item [n]`, then `tracker.item` for its caused_by / fixed_by
 *   neighbours (≤ 9) and the `duplicates` template, which the tracker graph draws;
 * - "Similar": `anchors {quoted: [title], words: ["similar"]}`.
 * Every read carries an AbortSignal and is dropped when its effect moves
 * on. URL state is `?mode=patterns&component=…&severity=…&issue=N`, written
 * with replaceState into the entry the Explorer pushed for the mode.
 */

export interface PatternsProps {
  identity: Identity;
  binding: TrackerBinding;
  /** The co-mention graph's entity vocabulary, to offer "walk from #n". */
  isEntity: (value: string) => boolean;
  onWalk: (entity: string) => void;
  /** Initial cell / issue from the URL. */
  initial: { component: string | null; severity: string | null; issue: number | null };
  onUrlState: (s: { component: string | null; severity: string | null; issue: number | null }) => void;
}

type PatternsTab = "board" | "graph";
const TABS: readonly PatternsTab[] = ["board", "graph"];

type IssuesStatus = "idle" | "loading" | "served" | "empty" | "tracker_unavailable" | "restricted_required" | "error";
type ItemStatus = "idle" | "loading" | "served" | "unknown" | "tracker_unavailable" | "error";

function sectionStatus(a: TrackerAnswer, template: string): { status: BoardStatus; rows: Record<string, unknown>[] } {
  const s = a.sections.find((x) => x.template === template);
  if (!s) return { status: "unplanned", rows: [] };
  if (s.status === "served") return { status: "served", rows: s.rows || [] };
  if (s.status === "empty") return { status: "empty", rows: [] };
  if (s.status === "tracker_unavailable") return { status: "tracker_unavailable", rows: [] };
  if (s.status === "restricted_required") return { status: "restricted_required", rows: [] };
  return { status: "empty", rows: s.rows || [] };
}

export function Patterns({ identity, binding, isEntity, onWalk, initial, onUrlState }: PatternsProps) {
  const tracker = useMemo(
    () => ({ org: identity.org, namespace: binding.tracker_namespace }),
    [identity.org, binding.tracker_namespace],
  );
  const [facets, setFacets] = useState<Facets>(emptyFacets);
  const [full, setFull] = useState<BoardModel | null>(null);
  const [boardStatus, setBoardStatus] = useState<BoardStatus>("loading");
  const [boardError, setBoardError] = useState<string | null>(null);
  const [cell, setCell] = useState<{ component: string; severity: string } | null>(
    initial.component !== null && initial.severity
      ? { component: initial.component, severity: initial.severity }
      : null,
  );
  const [area, setArea] = useState<{
    component: string;
    severity: string;
    sections: TrackerSection[];
    open: TrackerSection | null;
    status: IssuesStatus;
    error: string | null;
  } | null>(null);
  const [issueNumber, setIssueNumber] = useState<number | null>(initial.issue);
  const [items, setItems] = useState<Map<number, TrackerItem>>(new Map());
  const [itemStatus, setItemStatus] = useState<ItemStatus>("idle");
  const [duplicates, setDuplicates] = useState<DuplicateRow[]>([]);
  const [graphBusy, setGraphBusy] = useState(false);
  const [graphError, setGraphError] = useState<string | null>(null);
  // A deep link naming an issue opens on its tracker graph, not on the board it came from.
  const [tab, setTab] = useState<PatternsTab>(initial.issue !== null ? "graph" : "board");
  const [similar, setSimilar] = useState<{ for: string; hits: SimilarHit[] | null; busy: boolean }>({
    for: "",
    hits: null,
    busy: false,
  });
  const similarCtl = useRef<AbortController | null>(null);

  // The board and the gaps snapshot: once per namespace and window.
  useAbortableEffect(
    async (signal, live) => {
      // The last window's board stays up, marked busy, until this one answers.
      setBoardStatus("loading");
      setBoardError(null);
      const anchors = facets.days ? { days: facets.days } : {};
      try {
        const [board, gaps] = await Promise.all([
          trackerAnswer(tracker, { question: "bug board", anchors, limit: 50, signal }),
          trackerAnswer(tracker, {
            question: "test gaps",
            anchors: { days: facets.days || 365 },
            limit: 50,
            signal,
          }).catch(() => null),
        ]);
        if (!live()) return;
        const b = sectionStatus(board, "severity_board");
        const g = gaps ? sectionStatus(gaps, "test_gaps") : { rows: [] };
        setFull(buildBoard(b.rows, g.rows));
        setBoardStatus(b.status);
      } catch (e) {
        if (!live()) return;
        // The previous window's board is not this one's: no counts from it stay on screen.
        setFull(null);
        setBoardStatus("error");
        setBoardError(humanizeError(e));
      }
    },
    [tracker.org, tracker.namespace, facets.days],
  );

  const board = useMemo(() => (full ? filterBoard(full, facets) : null), [full, facets]);

  // A cell: the component's risk picture and the cell's open issues — the
  // severity rides in the anchors, so the server returns the cell itself.
  useAbortableEffect(
    async (signal, live) => {
      if (!cell) {
        setArea(null);
        return;
      }
      if (area && area.component === cell.component && area.severity === cell.severity && area.status !== "error")
        return;
      setArea({
        component: cell.component,
        severity: cell.severity,
        sections: [],
        open: null,
        status: "loading",
        error: null,
      });
      try {
        const a = await trackerAnswer(tracker, {
          anchors: { components: [cell.component], severities: [cell.severity], words: ["risk"] },
          limit: 50,
          signal,
        });
        if (!live()) return;
        const s = a.sections.find((x) => x.template === "area_open_issues") ?? null;
        const status: IssuesStatus = !s
          ? "empty"
          : s.status === "served"
            ? "served"
            : s.status === "tracker_unavailable"
              ? "tracker_unavailable"
              : s.status === "restricted_required"
                ? "restricted_required"
                : "empty";
        setArea({
          component: cell.component,
          severity: cell.severity,
          sections: a.sections,
          open: s,
          status,
          error: null,
        });
      } catch (e) {
        if (!live()) return;
        setArea({
          component: cell.component,
          severity: cell.severity,
          sections: [],
          open: null,
          status: "error",
          error: humanizeError(e),
        });
      }
    },
    // `area` is read only to skip a refetch of the cell already shown, never to trigger one.
    [cell?.component, cell?.severity, tracker.org, tracker.namespace],
  );

  const areaIsCell = Boolean(cell && area && area.component === cell.component && area.severity === cell.severity);
  const cellRead = useMemo(() => {
    if (!cell || !area || !areaIsCell || area.status !== "served") return null;
    return cellIssues(area.open, cell.severity, facets.flags);
  }, [cell, area, areaIsCell, facets.flags]);
  const issues: Issue[] | null = cellRead?.issues ?? null;

  const issue: Issue | null = useMemo(() => {
    if (issueNumber === null) return null;
    const fromList =
      issues?.find((i) => i.number === issueNumber) ??
      (area?.open
        ? issuesForCell(area.open.rows || [], null, new Set()).find((i) => i.number === issueNumber)
        : undefined);
    if (fromList) return fromList;
    const it = items.get(issueNumber);
    if (!it) return null;
    return {
      number: it.number,
      title: it.title,
      severity: it.tracker.severity || "unrated",
      labels: it.tracker.labels || [],
      component: it.tracker.component || "",
      untested: it.tracker.has_test !== true,
      flaky: Boolean(it.tracker.is_flaky_test),
      bisected: Boolean(it.tracker.bisect_outcome),
      bisect_outcome: it.tracker.bisect_outcome || "",
      url: it.url || it.tracker.url || "",
      state: it.state,
      opened_at: it.opened_at,
    };
  }, [issueNumber, issues, area, items]);

  // An issue: its record, its neighbours' titles, its duplicates.
  useAbortableEffect(
    async (signal, live) => {
      if (issueNumber === null) {
        setItemStatus("idle");
        setDuplicates([]);
        setGraphError(null);
        return;
      }
      setItemStatus("loading");
      setGraphBusy(true);
      setGraphError(null);
      try {
        const first = await trackerItem(tracker, [issueNumber], { signal });
        if (!live()) return;
        if (first.tracker_unavailable) {
          setItemStatus("tracker_unavailable");
          setGraphBusy(false);
          return;
        }
        const seed = first.items.find((i) => i.number === issueNumber);
        if (!seed) {
          setItemStatus("unknown");
          setGraphBusy(false);
          return;
        }
        setItems((m) => new Map(m).set(seed.number, seed));
        setItemStatus("served");
        const dupAnswer = await trackerAnswer(tracker, {
          question: "duplicates",
          anchors: { numbers: [issueNumber], words: ["similar"] },
          limit: 20,
          signal,
        }).catch(() => null);
        if (!live()) return;
        const dupRows = dupAnswer?.sections.find((s) => s.template === "duplicates")?.rows || [];
        const dups = dupRows.map(duplicateFrom).filter((d) => d.number > 0);
        setDuplicates(dups);
        const more = neighbourNumbers(seed, dups);
        if (more.length) {
          const second = await trackerItem(tracker, more, { signal });
          if (!live()) return;
          setItems((m) => {
            const n = new Map(m);
            for (const it of second.items) n.set(it.number, it);
            return n;
          });
        }
      } catch (e) {
        if (!live()) return;
        setItemStatus("error");
        setGraphError(humanizeError(e));
      } finally {
        if (live()) setGraphBusy(false);
      }
    },
    [issueNumber, tracker.org, tracker.namespace],
  );

  const graph = useMemo(
    () => (issueNumber !== null ? trackerGraphFrom(issueNumber, items, duplicates) : null),
    [issueNumber, items, duplicates],
  );

  const runSimilar = useCallback(
    (title: string) => {
      similarCtl.current?.abort();
      const ctl = new AbortController();
      similarCtl.current = ctl;
      setSimilar({ for: title, hits: null, busy: true });
      trackerAnswer(tracker, {
        anchors: { quoted: [title.slice(0, 200)], words: ["similar"] },
        limit: 10,
        signal: ctl.signal,
      })
        .then((a) => {
          if (ctl.signal.aborted) return;
          const rows = a.sections.find((s) => s.template === "similar_bugs")?.rows || [];
          setSimilar({
            for: title,
            busy: false,
            hits: rows.map((r) => ({
              number:
                Number(
                  String(r.source_id ?? "")
                    .split("#")
                    .pop(),
                ) || 0,
              title: String(r.title ?? ""),
              severity: String(r.severity ?? ""),
              component: String(r.component ?? ""),
              url: String(r.url ?? ""),
              open: Boolean(r.open),
              partial: Boolean(r.partial),
            })),
          });
        })
        .catch(() => {
          if (!ctl.signal.aborted) setSimilar({ for: title, hits: [], busy: false });
        });
    },
    [tracker],
  );
  useEffect(() => () => similarCtl.current?.abort(), []);

  // URL state rides the mode's history entry.
  useEffect(() => {
    onUrlState({ component: cell?.component ?? null, severity: cell?.severity ?? null, issue: issueNumber });
  }, [cell, issueNumber, onUrlState]);

  // Picking a cell shows the board it belongs to; picking an issue shows its
  // tracker graph — the picture follows the selection, the tab is not a second step.
  const pickCell = useCallback((c: { component: string; severity: string } | null) => {
    setCell(c);
    setIssueNumber(null);
    setTab("board");
    setSimilar({ for: "", hits: null, busy: false });
  }, []);
  const pickIssue = useCallback((n: number | null) => {
    setIssueNumber(n);
    if (n !== null) setTab("graph");
    setSimilar({ for: "", hits: null, busy: false });
  }, []);

  const cellRow = cell && full ? full.rows.find((r) => r.component === cell.component) : null;
  const cellCounts = cellRow && cell ? (cellRow.cells.get(cell.severity) ?? null) : null;
  const daysLabel = WINDOWS.find((w) => w.days === facets.days)?.label ?? "all open";
  const restricted = areaIsCell && area ? restrictedSections(area.sections) : [];
  const areaSections = areaIsCell && area ? area.sections : [];
  const graphHint = cell
    ? "Pick an issue in the inspector's list; its caused-by, fixed-by and duplicate edges are drawn here."
    : "Click a cell on the board, then an issue in its list; the tracker graph draws that issue's caused-by, fixed-by and duplicate edges.";

  return (
    <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[220px_minmax(0,1fr)_300px] lg:grid-rows-[minmax(0,1fr)] gap-4">
      <PatternsFacets
        board={full}
        facets={facets}
        onFacets={setFacets}
        trackerNamespace={binding.tracker_namespace}
        kind={binding.kind}
        restricted={binding.restricted}
      />

      <div className="min-w-0 min-h-0 flex flex-col">
        <Tabs
          value={tab}
          onValueChange={(v) => {
            const t = oneOf(TABS, v);
            if (t !== null) setTab(t);
          }}
          className="flex flex-col min-h-0"
        >
          <div className="h-8 mb-2 flex items-center justify-between gap-2 overflow-hidden">
            <Tabs.TriggersList aria-label="Patterns view">
              <TabTrigger value="board" icon="services">
                Board
              </TabTrigger>
              <TabTrigger value="graph" icon="git-merge">
                Tracker graph
                {graph ? <span className="tabular-nums">#{graph.seed.number}</span> : null}
              </TabTrigger>
            </Tabs.TriggersList>
            <div className="text-xs text-text-muted truncate" aria-live="polite">
              {boardStatus === "served" && board && full && (
                <>
                  <span className="tabular-nums text-text">{board.totalOpen}</span> open in{" "}
                  <span className="tabular-nums text-text">{board.rows.length}</span> component
                  {board.rows.length === 1 ? "" : "s"}
                  {board.rows.length !== full.rows.length ? ` (of ${full.rows.length})` : ""} · {daysLabel} · source{" "}
                  <span className="font-mono">{binding.tracker_namespace}</span>
                </>
              )}
            </div>
          </div>
          <Tabs.Content value="board" className="min-h-0">
            <Board
              board={board}
              status={boardStatus}
              error={boardError}
              totalRows={full?.rows.length}
              selected={cell}
              onSelect={pickCell}
              trackerNamespace={binding.tracker_namespace}
              daysLabel={daysLabel}
            />
          </Tabs.Content>
          <Tabs.Content value="graph" className="min-h-0">
            <TrackerGraph
              graph={graph}
              loading={graphBusy && !graph}
              error={graphError}
              hint={graphHint}
              selected={issueNumber}
              onSelect={(n) => {
                if (n !== null) pickIssue(n);
              }}
              onOpen={pickIssue}
            />
          </Tabs.Content>
        </Tabs>
      </div>

      <PatternsInspector
        trackerNamespace={binding.tracker_namespace}
        cell={cell ? { ...cell, label: cellRow?.label ?? cell.component ?? "(no component)" } : null}
        cellCounts={cellCounts}
        issues={issues}
        issuesRead={cellRead?.read ?? 50}
        issuesCapped={cellRead?.capped ?? false}
        issuesStatus={cell ? (areaIsCell && area ? area.status : "loading") : "idle"}
        issuesError={area?.error ?? null}
        restricted={restricted}
        sections={areaSections}
        restrictedNamespace={binding.restricted}
        duplicates={duplicates}
        issue={issue}
        item={issueNumber !== null ? (items.get(issueNumber) ?? null) : null}
        itemStatus={itemStatus}
        neighbours={items}
        similar={similar.hits}
        similarBusy={similar.busy}
        similarFor={similar.for || null}
        isEntity={(n) => isEntity(String(n))}
        onPickIssue={pickIssue}
        onShowGraph={() => setTab("graph")}
        onSimilar={runSimilar}
        onWalk={onWalk}
        onClearCell={() => pickCell(null)}
      />
    </div>
  );
}

export type { Window };
