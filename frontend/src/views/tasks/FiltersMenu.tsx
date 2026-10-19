import { Button } from "@clickhouse/click-ui/Button";
import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import { MenuChoice, MenuChoiceGroup } from "../../components/MenuChoice";
import { MenuTrigger } from "../../components/ui/Menu";
import { ATTENTIONS, ATTENTION_META, type Attention, type Filters, type PosterFilter } from "../../lib/tasks";

/** Every kind or poster: "" or null is not a value a Select can hold. */
export const ANY = "*";

export const POSTED_BY: ReadonlyArray<readonly [PosterFilter | typeof ANY, string]> = [
  [ANY, "Anyone"],
  ["me", "This agent"],
  ["others", "Other posters"],
];

/**
 * A phone's filters: the four "Needs you" tiles (each with its count), the kind and the
 * poster as one Filters menu beside the inbox, since a row of 44 px tiles under a touch
 * pointer pushes the first task past the chrome budget. The trigger counts the filters on.
 */
export function FiltersMenu({
  filters,
  counts,
  kinds,
  onChange,
}: {
  filters: Filters;
  /** Null while the rows load or could not be read. */
  counts: Record<Attention, number> | null;
  kinds: string[];
  onChange: (next: Partial<Filters>) => void;
}) {
  const on = [filters.attention, filters.kind, filters.poster].filter((v) => v !== null).length;
  return (
    <Dropdown modal={false}>
      <MenuTrigger>
        <Button type="secondary" htmlType="button" iconRight="chevron-down" className="shrink-0">
          {on > 0 ? `Filters (${on})` : "Filters"}
        </Button>
      </MenuTrigger>
      <Dropdown.Content align="end" aria-label="Filters" aria-labelledby={undefined}>
        <MenuChoiceGroup label="Needs you">
          <MenuChoice radio checked={filters.attention === null} onSelect={() => onChange({ attention: null })}>
            Everything
          </MenuChoice>
          {ATTENTIONS.map((k) => (
            <MenuChoice key={k} radio checked={filters.attention === k} onSelect={() => onChange({ attention: k })}>
              {counts ? `${ATTENTION_META[k].label} (${counts[k]})` : ATTENTION_META[k].label}
            </MenuChoice>
          ))}
        </MenuChoiceGroup>
        {(kinds.length > 1 || filters.kind) && (
          <MenuChoiceGroup label="Kind">
            <MenuChoice radio checked={filters.kind === null} onSelect={() => onChange({ kind: null })}>
              Every kind
            </MenuChoice>
            {kinds.map((k) => (
              <MenuChoice key={k} radio checked={filters.kind === k} onSelect={() => onChange({ kind: k })}>
                {k}
              </MenuChoice>
            ))}
          </MenuChoiceGroup>
        )}
        <MenuChoiceGroup label="Posted by">
          {POSTED_BY.map(([v, label]) => (
            <MenuChoice
              key={v}
              radio
              checked={(filters.poster ?? ANY) === v}
              onSelect={() => onChange({ poster: v === ANY ? null : v })}
            >
              {label}
            </MenuChoice>
          ))}
        </MenuChoiceGroup>
      </Dropdown.Content>
    </Dropdown>
  );
}
