import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { CircleDot, Footprints, LayoutGrid, Route, Waypoints } from "lucide-react";
import type { Identity, TaxonomyEntry } from "../../api";
import { ScopeBadge } from "../../components/ScopeBadge";
import { matchEntities, parsePathQuery, shorten } from "./model";

/**
 * Bar — the top strip: scope, the omnibox, Walk, Path, and the trail.
 *
 * The omnibox suggests from the taxonomy's top 500 entities (prefix
 * matches first, then substring, each with its mention count). Enter walks the highlighted
 * suggestion or the typed text; `a → b` / `a -> b` (or the Path button)
 * finds a path instead. The trail is the seeds walked this session,
 * newest last, each a button, backed by the same `history.pushState` entries.
 */

export interface BarProps {
  identity: Identity;
  entities: TaxonomyEntry[] | null;
  draft: string;
  onDraft: (s: string) => void;
  onWalk: (seed: string) => void;
  onPath: (a: string, b: string) => void;
  trail: string[];
  currentSeed: string | null;
  busy: boolean;
  /** Patterns mode exists on QA-bound namespaces only. */
  patternsAvailable: boolean;
  patterns: boolean;
  onPatterns: (on: boolean) => void;
}

type Suggestion = { value: string; count: number; how: "prefix" | "substring" };

export function Bar({
  identity,
  entities,
  draft,
  onDraft,
  onWalk,
  onPath,
  trail,
  currentSeed,
  busy,
  patternsAvailable,
  patterns,
  onPatterns,
}: BarProps) {
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(0);
  // The list closes a beat after blur so a click on an option lands first.
  const blurTimer = useRef<number | undefined>(undefined);
  useEffect(() => {
    const t = blurTimer;
    return () => window.clearTimeout(t.current);
  }, []);
  const pathQuery = parsePathQuery(draft);
  // The half being typed: the second end of `a → b`, else the whole text.
  const typing = (pathQuery ? draft.split(/→|->/).pop() || "" : draft).trim().toLowerCase();

  const suggestions = useMemo<Suggestion[]>(() => {
    if (!entities) return [];
    return matchEntities(entities, typing, 8).map((e) => ({
      value: e.value,
      count: e.count,
      how: e.value.startsWith(typing) ? "prefix" : "substring",
    }));
  }, [typing, entities]);

  useEffect(() => {
    setHi(0);
  }, [draft, suggestions.length]);

  const submit = (s?: Suggestion) => {
    setOpen(false);
    if (s) {
      const entity = s.value;
      if (pathQuery) {
        onDraft(`${pathQuery.a} → ${entity}`);
        onPath(pathQuery.a, entity);
      } else {
        onDraft(entity);
        onWalk(entity);
      }
      return;
    }
    if (pathQuery) {
      onPath(pathQuery.a, pathQuery.b);
      return;
    }
    if (draft.trim()) onWalk(draft);
  };

  const onKey = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" && suggestions.length) {
      e.preventDefault();
      setOpen(true);
      setHi((h) => Math.min(h + 1, suggestions.length - 1));
    } else if (e.key === "ArrowUp" && suggestions.length) {
      e.preventDefault();
      setHi((h) => Math.max(h - 1, 0));
    } else if (e.key === "Escape") {
      setOpen(false);
    } else if (e.key === "Enter") {
      e.preventDefault();
      submit(open && suggestions.length ? suggestions[hi] : undefined);
    }
  };

  const pathTarget = pathQuery
    ? null
    : currentSeed && draft.trim() && draft.trim().toLowerCase() !== currentSeed
      ? draft.trim().toLowerCase()
      : null;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-2xl font-semibold tracking-tight inline-flex items-center gap-2">
          <Waypoints size={18} /> Graph
        </h1>
        <ScopeBadge kind="namespace" value={identity.namespace} />
        <form
          className="flex items-center gap-2 flex-1 min-w-[280px]"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            submit(open && suggestions.length ? suggestions[hi] : undefined);
          }}
        >
          <div className="relative flex-1 max-w-[520px]">
            {/* An entity box, not a search box: a node glyph and an
                entity placeholder, so it does not read as the header's ⌘K search. */}
            <CircleDot
              size={14}
              aria-hidden="true"
              className="absolute left-2 top-1/2 -translate-y-1/2 text-text-muted"
            />
            <input
              id="graph-seed"
              role="combobox"
              aria-expanded={open && suggestions.length > 0}
              aria-controls="graph-omnibox-list"
              aria-autocomplete="list"
              aria-activedescendant={open && suggestions.length ? `graph-omnibox-${hi}` : undefined}
              value={draft}
              onChange={(e) => {
                onDraft(e.target.value);
                setOpen(true);
              }}
              onFocus={() => setOpen(true)}
              onBlur={() => {
                window.clearTimeout(blurTimer.current);
                blurTimer.current = window.setTimeout(() => setOpen(false), 120);
              }}
              onKeyDown={onKey}
              placeholder="Entity, or a → b"
              aria-label="Seed entity, or two entities joined by an arrow for a path"
              autoComplete="off"
              spellCheck={false}
              className="w-full pl-7 pr-2 py-1.5 text-sm rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] font-mono"
            />
            {open && (suggestions.length > 0 || (typing.length >= 3 && entities !== null)) && (
              <ul
                id="graph-omnibox-list"
                role="listbox"
                className="absolute z-30 left-0 right-0 mt-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] shadow-xl overflow-hidden text-xs"
              >
                {suggestions.map((s, i) => (
                  <li
                    key={s.value}
                    id={`graph-omnibox-${i}`}
                    role="option"
                    aria-selected={i === hi}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      submit(s);
                    }}
                    onMouseEnter={() => setHi(i)}
                    className={`flex items-center gap-2 px-2.5 py-1.5 cursor-pointer ${i === hi ? "bg-[var(--color-surface-2)]" : ""}`}
                  >
                    <span className="font-mono truncate flex-1">{s.value}</span>
                    <span className="text-2xs text-[var(--color-text-subtle)]">{s.how}</span>
                    <span className="tabular-nums text-[var(--color-text-muted)] w-14 text-right">{s.count}</span>
                  </li>
                ))}
                {suggestions.length === 0 && (
                  <li className="px-2.5 py-1.5 text-[var(--color-text-subtle)]">
                    No entity matches “{typing}”; Walk tries it as typed (the server canonicalises).
                  </li>
                )}
                {suggestions.length > 0 && pathQuery && (
                  <li className="px-2.5 py-1 text-2xs text-[var(--color-text-subtle)] border-t border-[var(--color-border)]">
                    ↵ finds the path from <span className="font-mono">{pathQuery.a}</span> to the highlighted entity
                  </li>
                )}
              </ul>
            )}
          </div>
          <button
            type="submit"
            disabled={!draft.trim() || busy}
            className="inline-flex items-center gap-1.5 text-sm rounded-lg bg-[var(--color-accent)] text-[var(--color-on-accent)] px-3 py-1.5 font-medium hover:bg-[var(--color-accent-strong)] disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {pathQuery ? (
              <>
                <Route size={13} /> Path
              </>
            ) : (
              <>
                <Footprints size={13} /> Walk
              </>
            )}
          </button>
          {!pathQuery && (
            <button
              type="button"
              disabled={!pathTarget || busy}
              onClick={() => {
                if (pathTarget && currentSeed) {
                  onDraft(`${currentSeed} → ${pathTarget}`);
                  onPath(currentSeed, pathTarget);
                }
              }}
              className="inline-flex items-center gap-1.5 text-sm rounded-lg border border-[var(--color-border-strong)] px-3 py-1.5 hover:bg-[var(--color-surface-2)] disabled:opacity-50 disabled:cursor-not-allowed"
              aria-label={pathTarget && currentSeed ? `Path from ${currentSeed} to ${pathTarget}` : "Path"}
              aria-describedby={pathTarget && currentSeed ? undefined : "explorer-path-hint"}
            >
              <Route size={13} /> Path
            </button>
          )}
        </form>
        {patternsAvailable && (
          <button
            type="button"
            aria-pressed={patterns}
            onClick={() => onPatterns(!patterns)}
            className={`inline-flex items-center gap-1.5 text-sm rounded-lg border px-3 py-1.5 ${patterns ? "border-text bg-surface-2 text-text" : "border-border-strong text-text-muted hover:bg-surface-2 hover:text-text"}`}
            aria-describedby="explorer-patterns-hint"
          >
            <LayoutGrid size={13} aria-hidden="true" /> Patterns
          </button>
        )}
        <span id="explorer-patterns-hint" className="sr-only">
          Open issues by component and severity from the tracker, and the issue and PR graph behind a cell. Press again
          to return to the entity graph.
        </span>
        <span id="explorer-path-hint" className="sr-only">
          Type a second entity while a walk is open, or a → b, to ask for a path.
        </span>
      </div>
      {trail.length > 0 && (
        <div
          className="text-xs text-[var(--color-text-muted)] flex items-center gap-1 flex-wrap"
          aria-label="Seeds walked this session"
        >
          <span>walked:</span>
          {trail.map((t, i) => (
            <span key={`${t}-${i}`} className="inline-flex items-center gap-1">
              {i > 0 && <span className="text-[var(--color-text-subtle)]">›</span>}
              <button
                type="button"
                onClick={() => onWalk(t)}
                disabled={t === currentSeed}
                className={`font-mono ${t === currentSeed ? "text-[var(--color-text)]" : "underline hover:text-[var(--color-text)]"}`}
              >
                {shorten(t, 24)}
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
