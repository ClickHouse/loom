import type { ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Select } from "@clickhouse/click-ui/Select";
import type { TaxonomyResponse } from "../../api";
import { Chip, SectionHeading, TipTarget } from "../../components/ui";
import { COMMUNITY_PALETTE, HOP_COLORS, HOP_EMPHASIS, type LabelMode } from "./Canvas";
import { PROVENANCE_TIERS, relationLabel, today, type EdgeFilters, type Provenance } from "./model";
import { communitiesCopy } from "./notices";
import { oneOf } from "../../lib/oneOf";

/**
 * Facets — the left pane (220 px): what colours, filters and configures
 * the walk, none of it a read. Communities from `namespace.taxonomy`
 * (colour; click = walk from the community's label). Relation and
 * provenance chips filter edges client-side now and ride the next
 * `facts.traverse` as `relations[]` / `labeled`. Memory types and tags
 * narrow the inspector's memory list — their counts are the namespace's,
 * not this walk's, and the heading says so. Valid-as-of replays the walk.
 * The walk settings (hops, per hop, typed only, labels) moved here from
 * the header. Changing any facet never clears the selection.
 *
 * The pane owns its scroll; every heading is a `SectionHeading` (one line, hint in a
 * tooltip, never truncated); every toggle is a `Chip` (28 px target, count spoken with
 * its qualifier).
 *
 * The Communities section says how many are shown and when they were computed (absolute
 * time in a tooltip); with none, the Colour section says what computes them and when, in
 * a sentence that wraps and is never clamped.
 */

export interface FacetsProps {
  taxonomy: TaxonomyResponse | null;
  seed: string | null;
  relations: Array<{ relation: string; count: number }>;
  filters: EdgeFilters;
  onFilters: (f: EdgeFilters) => void;
  filteredOut: number;
  memoryTypes: Set<string>;
  onMemoryTypes: (s: Set<string>) => void;
  tags: Set<string>;
  onTags: (s: Set<string>) => void;
  asOf: string;
  onAsOf: (d: string) => void;
  hops: number;
  onHops: (n: number) => void;
  perHop: number;
  onPerHop: (n: number) => void;
  typed: boolean;
  onTyped: (b: boolean) => void;
  labels: LabelMode;
  onLabels: (m: LabelMode) => void;
  onWalk: (entity: string) => void;
  /** One short line when the namespace has no QA binding. */
  patternsNote?: string | null;
}

function toggle<T>(s: Set<T>, v: T): Set<T> {
  const n = new Set(s);
  if (n.has(v)) n.delete(v);
  else n.add(v);
  return n;
}

function Section({
  label,
  count,
  caption,
  hint,
  actions,
  children,
}: {
  label: string;
  count?: number;
  caption?: string;
  hint?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="pb-3 mb-3 border-b border-border last:border-b-0" aria-label={label}>
      <SectionHeading level={2} label={label} count={count} caption={caption} hint={hint} actions={actions} />
      {children}
    </section>
  );
}

const LABEL_MODES: ReadonlyArray<{ value: LabelMode; label: string }> = [
  { value: "key", label: "strongest" },
  { value: "all", label: "all" },
  { value: "none", label: "none" },
];

const IN_NAMESPACE_HINT =
  "Counts are memories in the whole namespace, not in this walk. A chip narrows the inspector's memory list to that type or tag.";

export function Facets({
  taxonomy,
  seed,
  relations,
  filters,
  onFilters,
  filteredOut,
  memoryTypes,
  onMemoryTypes,
  tags,
  onTags,
  asOf,
  onAsOf,
  hops,
  onHops,
  perHop,
  onPerHop,
  typed,
  onTyped,
  labels,
  onLabels,
  onWalk,
  patternsNote,
}: FacetsProps) {
  const communities = taxonomy?.communities || [];
  const commCopy = communitiesCopy(taxonomy);
  // The aside scrolls, so it clips its overflow: 4 px each side (the left one drawn into the
  // gap beside it) is the controls' focus-ring room.
  return (
    <aside aria-label="Facets" className="text-xs min-h-0 overflow-y-auto overscroll-contain -ml-1 px-1">
      {/* No hint on this heading: its (i) would be named "About Walk", and the
          e2e graph spec finds the Walk button by a substring of its name. */}
      <Section label="Walk">
        <div className="grid grid-cols-2 gap-x-2 gap-y-1.5">
          <Select id="graph-hops" label="Hops" value={String(hops)} onSelect={(v) => onHops(Number(v))}>
            {[1, 2, 3].map((h) => (
              <Select.Item key={h} value={String(h)}>
                {h}
              </Select.Item>
            ))}
          </Select>
          <Select id="graph-per-hop" label="Per hop" value={String(perHop)} onSelect={(v) => onPerHop(Number(v))}>
            {[6, 12, 20].map((k) => (
              <Select.Item key={k} value={String(k)}>
                {k}
              </Select.Item>
            ))}
          </Select>
          <div className="col-span-2">
            <Select
              id="graph-labels"
              label="Labels"
              value={labels}
              onSelect={(value) => {
                const v = oneOf(
                  LABEL_MODES.map((o) => o.value),
                  value,
                );
                if (v !== null) onLabels(v);
              }}
            >
              {LABEL_MODES.map((o) => (
                <Select.Item key={o.value} value={o.value}>
                  {o.label}
                </Select.Item>
              ))}
            </Select>
          </div>
          <div className="col-span-2 min-h-7 flex items-center">
            <Checkbox
              id="graph-typed"
              label="Typed edges only"
              checked={typed}
              onCheckedChange={(v) => onTyped(v === true)}
            />
          </div>
        </div>
      </Section>

      <Section
        label="Valid as of"
        caption={asOf ? undefined : "now"}
        hint="Replays the walk over the edges that were valid at the end of the chosen day."
        actions={
          asOf ? (
            <Button type="empty" htmlType="button" onClick={() => onAsOf("")}>
              Now
            </Button>
          ) : undefined
        }
      >
        <input
          id="graph-as-of"
          type="date"
          value={asOf}
          max={today()}
          aria-label="Valid as of"
          aria-describedby="graph-as-of-hint"
          onChange={(e) => onAsOf(e.target.value)}
        />
        <span id="graph-as-of-hint" className="sr-only">
          Replay the graph as it stood at the end of this day
        </span>
      </Section>

      {communities.length > 0 && (
        <Section
          label="Communities"
          count={communities.length}
          hint="Colour is the entity's community, a group of entities often mentioned together, recomputed every 6 hours for namespaces explored in the last 14 days; click one to walk from its label."
        >
          {commCopy.kind === "some" && (
            <p className="caption mb-1.5 break-words" data-testid="communities-computed">
              {commCopy.absolute ? (
                <TipTarget tip={`Computed ${commCopy.absolute}`} underline>
                  {commCopy.line}
                </TipTarget>
              ) : (
                commCopy.line
              )}
            </p>
          )}
          <div className="space-y-0.5">
            {communities.map((c) => (
              <button
                key={c.community_id}
                type="button"
                onClick={() => onWalk(c.label)}
                className="w-full text-left flex items-center gap-2 hover:bg-surface-2 rounded px-1 min-h-7"
              >
                <span
                  className="inline-block w-2.5 h-2.5 rounded-full shrink-0"
                  style={{ background: COMMUNITY_PALETTE[Math.abs(c.community_id) % COMMUNITY_PALETTE.length] }}
                  aria-hidden="true"
                />
                <span className="min-w-0 flex-1 break-words">{c.label}</span>
                <span className="text-text-muted tabular-nums">{c.size}</span>
              </button>
            ))}
          </div>
        </Section>
      )}
      {communities.length === 0 && (
        <Section
          label="Colour"
          caption="by hop"
          hint="Communities, groups of entities often mentioned together, are found every 6 hours for namespaces explored in the last 14 days, when an operator of this deployment has turned them on; until a pass has run here, each ring is coloured by its hop."
        >
          <ul className="space-y-1 text-text-muted">
            <li className="flex items-center gap-2">
              <span
                className="inline-block w-2.5 h-2.5 rounded-full"
                style={{ background: HOP_COLORS[0] }}
                aria-hidden="true"
              />{" "}
              seed
            </li>
            {[1, 2, 3].slice(0, hops).map((h) => (
              <li key={h} className="flex items-center gap-2">
                <span
                  className="inline-block w-2.5 h-2.5 rounded-full"
                  style={{ background: HOP_COLORS[h], opacity: HOP_EMPHASIS[h] }}
                  aria-hidden="true"
                />{" "}
                {h} hop{h === 1 ? "" : "s"} away
              </li>
            ))}
          </ul>
          <p className="caption mt-1.5 break-words whitespace-normal" data-testid="communities-none">
            {commCopy.line}
          </p>
        </Section>
      )}

      <Section
        label="Relations"
        count={relations.length || undefined}
        caption={filters.relations.size ? `${filters.relations.size} on` : undefined}
        hint="Relations on this walk's typed edges. A chip hides every other edge now, client-side, and rides the next walk as relations[]."
      >
        {relations.length === 0 ? (
          <p className="text-text-muted">
            {seed
              ? "This walk has no typed edges; tick “typed edges only” to walk the typed facts."
              : "Chips appear once a walk has typed edges; a chip set now rides the next walk."}
          </p>
        ) : (
          <div className="flex flex-wrap gap-1">
            {relations.map((r) => (
              <Chip
                key={r.relation}
                label={relationLabel(r.relation)}
                count={r.count}
                countLabel="edges"
                selected={filters.relations.has(r.relation)}
                onClick={() => onFilters({ ...filters, relations: toggle(filters.relations, r.relation) })}
              />
            ))}
          </div>
        )}
      </Section>

      <Section
        label="Provenance"
        caption={filters.provenance.size ? `${filters.provenance.size} on` : undefined}
        hint="How a typed edge was produced: extracted from text, inferred by the fold, or a facet of a code index. A chip keeps only those edges and rides the next walk as labeled."
        actions={
          filters.relations.size > 0 || filters.provenance.size > 0 ? (
            <Button
              type="empty"
              htmlType="button"
              onClick={() => onFilters({ relations: new Set(), provenance: new Set() })}
            >
              Clear chips
            </Button>
          ) : undefined
        }
      >
        <div className="flex flex-wrap gap-1">
          {PROVENANCE_TIERS.map((t: Provenance) => (
            <Chip
              key={t}
              label={t}
              selected={filters.provenance.has(t)}
              onClick={() => onFilters({ ...filters, provenance: toggle(filters.provenance, t) })}
            />
          ))}
        </div>
        {filteredOut > 0 && (
          <p className="mt-1.5 text-text-muted">
            <span className="tabular-nums">{filteredOut}</span> node{filteredOut === 1 ? "" : "s"} hidden by chips;
            pinned nodes stay.
          </p>
        )}
      </Section>

      {taxonomy && taxonomy.memory_types.length > 0 && (
        <Section label="Memory types" caption="in namespace" hint={IN_NAMESPACE_HINT}>
          <div className="flex flex-wrap gap-1">
            {taxonomy.memory_types.map((t) => (
              <Chip
                key={t.value}
                label={t.value}
                count={t.count}
                countLabel="in namespace"
                selected={memoryTypes.has(t.value)}
                onClick={() => onMemoryTypes(toggle(memoryTypes, t.value))}
              />
            ))}
          </div>
        </Section>
      )}
      {taxonomy && taxonomy.tags.length > 0 && (
        <Section label="Tags" caption="in namespace" hint={IN_NAMESPACE_HINT}>
          <div className="flex flex-wrap gap-1">
            {taxonomy.tags.slice(0, 20).map((t) => (
              <Chip
                key={t.value}
                label={t.value}
                count={t.count}
                countLabel="in namespace"
                selected={tags.has(t.value)}
                onClick={() => onTags(toggle(tags, t.value))}
              />
            ))}
          </div>
        </Section>
      )}

      {patternsNote && <p className="caption text-text-muted pb-3 mb-3 border-b border-border">{patternsNote}</p>}

      {taxonomy && taxonomy.entities.length > 0 && (
        <Section
          label="Most mentioned"
          count={Math.min(24, taxonomy.entities.length)}
          hint="The entities the namespace's memories name most, with how many memories mention each; click one to walk from it."
        >
          <div className="flex flex-wrap gap-1">
            {taxonomy.entities.slice(0, 24).map((e) => (
              <Chip
                key={e.value}
                label={e.value}
                count={e.count}
                countLabel="memories"
                mono
                selected={seed === e.value}
                onClick={() => onWalk(e.value)}
              />
            ))}
          </div>
        </Section>
      )}
    </aside>
  );
}
