import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Icon } from "@clickhouse/click-ui/Icon";
import { TextField } from "@clickhouse/click-ui/TextField";
import type { Identity, TaxonomyEntry } from "../../api";
import { ScopeBadge } from "../../components/ScopeBadge";
import { matchEntities, parsePathQuery, shorten } from "./model";
import { PageHeader, Unavailable } from "../../components/ui";

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
      <PageHeader title="Graph" tab={patterns ? "Patterns" : undefined}>
        <ScopeBadge kind="namespace" value={identity.namespace} />
        {/* biome-ignore lint/a11y/useSemanticElements: the box submits, so it stays a <form>; role="search" makes it the search landmark */}
        <form
          className="flex flex-wrap items-center gap-2 flex-1 min-w-[280px]"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            submit(open && suggestions.length ? suggestions[hi] : undefined);
          }}
        >
          {/* Below sm the field takes the whole row, so its placeholder is not cut. */}
          <div className="relative basis-full sm:basis-auto sm:flex-1 sm:max-w-[520px]">
            {/* An entity box, not a search box: a node glyph and an
                entity placeholder, so it does not read as the header's search. */}
            <TextField
              id="graph-seed"
              role="combobox"
              aria-expanded={open && suggestions.length > 0}
              aria-controls="graph-omnibox-list"
              aria-autocomplete="list"
              aria-activedescendant={open && suggestions.length ? `graph-omnibox-${hi}` : undefined}
              value={draft}
              onChange={(value) => {
                onDraft(value);
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
              startContent={<Icon name="dot" size="sm" aria-hidden="true" />}
            />
            {open && (suggestions.length > 0 || (typing.length >= 3 && entities !== null)) && (
              <div className="absolute z-30 left-0 right-0 mt-1 rounded-lg border border-border bg-surface shadow-xl overflow-hidden text-xs">
                {/* The listbox holds options only; the messages below it are not choices. */}
                <ul
                  id="graph-omnibox-list"
                  // biome-ignore lint/a11y/noNoninteractiveElementToInteractiveRole: APG combobox with a listbox popup — focus stays on the input, aria-activedescendant names the option
                  role="listbox"
                  aria-label="Matching entities"
                  hidden={suggestions.length === 0}
                >
                  {suggestions.map((s, i) => (
                    // biome-ignore lint/a11y/useFocusableInteractive: APG combobox option — never focused itself, the input points at it with aria-activedescendant
                    <li
                      key={s.value}
                      id={`graph-omnibox-${i}`}
                      // biome-ignore lint/a11y/noNoninteractiveElementToInteractiveRole: APG combobox option inside the listbox above
                      role="option"
                      aria-selected={i === hi}
                      onMouseDown={(e) => {
                        e.preventDefault();
                        submit(s);
                      }}
                      onMouseEnter={() => setHi(i)}
                      className={`flex items-center gap-2 px-2.5 py-1.5 cursor-pointer ${i === hi ? "bg-surface-2" : ""}`}
                    >
                      <span className="font-mono truncate flex-1">{s.value}</span>
                      <span className="text-xs text-text-muted">{s.how}</span>
                      <span className="tabular-nums text-text-muted w-14 text-right">{s.count}</span>
                    </li>
                  ))}
                </ul>
                {suggestions.length === 0 && (
                  <p role="status" className="px-2.5 py-1.5 text-text-muted">
                    No entity matches “{typing}”; Walk tries it as typed (the server canonicalises).
                  </p>
                )}
                {suggestions.length > 0 && pathQuery && (
                  <p className="px-2.5 py-1 text-xs text-text-muted border-t border-border">
                    ↵ finds the path from <span className="font-mono">{pathQuery.a}</span> to the highlighted entity
                  </p>
                )}
              </div>
            )}
          </div>
          <Button
            type="primary"
            htmlType="submit"
            iconLeft={pathQuery ? "arrow-directions" : "arrow-right"}
            disabled={!draft.trim() || busy}
          >
            {pathQuery ? "Path" : "Walk"}
          </Button>
          {!pathQuery && (
            <Unavailable
              reason={
                busy
                  ? "A walk is loading."
                  : pathTarget && currentSeed
                    ? null
                    : "Type a second entity while a walk is open, or a → b, to ask for a path."
              }
            >
              <Button
                type="secondary"
                htmlType="button"
                iconLeft="arrow-directions"
                onClick={() => {
                  if (pathTarget && currentSeed) {
                    onDraft(`${currentSeed} → ${pathTarget}`);
                    onPath(currentSeed, pathTarget);
                  }
                }}
                aria-label={pathTarget && currentSeed ? `Path from ${currentSeed} to ${pathTarget}` : "Path"}
              >
                Path
              </Button>
            </Unavailable>
          )}
        </form>
        {patternsAvailable && (
          <ButtonGroup
            aria-label="Mode"
            selected={patterns ? "patterns" : ""}
            onClick={() => onPatterns(!patterns)}
            options={[
              {
                value: "patterns",
                label: (
                  <span className="inline-flex items-center gap-1.5">
                    <Icon name="services" size="xs" aria-hidden="true" />
                    Patterns
                  </span>
                ),
                "aria-describedby": "explorer-patterns-hint",
              },
            ]}
          />
        )}
        <span id="explorer-patterns-hint" className="sr-only">
          Open issues by component and severity from the tracker, and the issue and PR graph behind a cell. Press again
          to return to the entity graph.
        </span>
      </PageHeader>
      {trail.length > 0 && (
        <div className="text-xs text-text-muted flex items-center gap-1 flex-wrap">
          <span>walked:</span>
          {trail.map((t, i) => (
            <span key={`${t}-${i}`} className="inline-flex items-center gap-1">
              {i > 0 && <span className="text-text-muted">›</span>}
              <button
                type="button"
                onClick={() => onWalk(t)}
                disabled={t === currentSeed}
                className={`font-mono ${t === currentSeed ? "text-text" : "underline hover:text-text"}`}
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
