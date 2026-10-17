/**
 * The Map step — the person's own tables onto loom's model.
 *
 * Loom proposed a mapping from the tables it read (`tracker.source.inspect`).
 * Here the person confirms or changes it: which table holds the items (and,
 * optionally, the pull requests and the links), and for each field loom reads,
 * which of their columns holds it. A reference ('Org/repo#number') is built
 * from a repository column or a fixed repository, plus a number column — the
 * two shapes almost every tracker has — never typed as an expression. Fields
 * left on "Loom default" are filled in by loom (typed defaults, or derived:
 * a PR's state from merged/closed …). Nothing here is SQL.
 */
import { useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Label } from "@clickhouse/click-ui/Label";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  describeExpr,
  refExpr,
  refParts,
  ROLE_FIELDS,
  ROLE_LABEL,
  type FieldSpec,
  type InspectedTable,
  type LogicalTable,
  type MapExpr,
  type MappingProposal,
  type SourceMapping,
  type TableMapping,
} from "../lib/trackerSources";

const TYPE_VALUES = [
  "fixes",
  "related_to",
  "duplicate_of",
  "caused_by",
  "backport_of",
  "reverts",
  "github_pr",
  "github_issue",
  "core_incident",
];
const ROLES: LogicalTable[] = ["github_issues", "github_prs", "edges"];

function columnsOf(tables: InspectedTable[], source: string | undefined): { name: string; type: string }[] {
  if (!source) return [];
  const bare = source.includes(".") ? source.split(".").pop() : source;
  return tables.find((t) => t.table === bare)?.columns ?? [];
}

const shortType = (t: string) => t.replace(/^LowCardinality\((.*)\)$/, "$1").replace(/^Nullable\((.*)\)$/, "$1?");

export function MappingEditor({
  database,
  tables,
  mapping,
  proposal,
  onChange,
}: {
  database: string;
  tables: InspectedTable[];
  mapping: SourceMapping;
  proposal: MappingProposal | null;
  onChange: (m: SourceMapping) => void;
}) {
  const setRole = (role: LogicalTable, t: TableMapping | undefined) => {
    const next: SourceMapping = { version: 1, tables: { ...mapping.tables } };
    if (t) next.tables[role] = t;
    else delete next.tables[role];
    onChange(next);
  };
  return (
    <div className="space-y-3">
      {ROLES.map((role) => (
        <RoleCard
          key={role}
          role={role}
          database={database}
          tables={tables}
          value={mapping.tables[role]}
          proposal={proposal}
          onChange={(t) => setRole(role, t)}
        />
      ))}
    </div>
  );
}

function RoleCard({
  role,
  database,
  tables,
  value,
  proposal,
  onChange,
}: {
  role: LogicalTable;
  database: string;
  tables: InspectedTable[];
  value: TableMapping | undefined;
  proposal: MappingProposal | null;
  onChange: (t: TableMapping | undefined) => void;
}) {
  const [more, setMore] = useState(false);
  const label = ROLE_LABEL[role];
  const cols = columnsOf(tables, value?.source);
  const colNames = new Set(cols.map((c) => c.name));
  const suggested = proposal?.fields?.[role] ?? {};
  const pick = (table: string) => {
    if (!table) {
      onChange(undefined);
      return;
    }
    const t = tables.find((x) => x.table === table);
    const names = new Set((t?.columns ?? []).map((c) => c.name));
    // keep the fields whose columns the new table also has
    const kept: Record<string, MapExpr> = {};
    for (const [k, e] of Object.entries(value?.columns ?? {})) {
      if (typeof e === "string" ? names.has(e) : true) kept[k] = e;
    }
    onChange({
      source: `${database}.${table}`,
      final: /Replacing|Collapsing|Aggregating|Summing/.test(t?.engine ?? ""),
      columns: kept,
    });
  };
  const setField = (key: string, e: MapExpr | undefined) => {
    if (!value) return;
    const columns = { ...value.columns };
    if (e === undefined || e === "") delete columns[key];
    else columns[key] = e;
    onChange({ ...value, columns });
  };
  const fields = ROLE_FIELDS[role];
  const shown = fields.filter((f) => f.core || more || value?.columns[f.key] !== undefined);
  const hidden = fields.length - shown.length;
  const id = `map-${role}`;
  return (
    <section aria-labelledby={`${id}-h`} className="rounded-lg border border-border p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id={`${id}-h`}>{label.title}</h3>
        <span className="text-xs text-text-muted min-w-0">
          — {label.hint}
          {label.required ? "" : " (optional)"}
        </span>
        <Select
          id={`${id}-table`}
          className="ml-auto w-64 max-w-full"
          triggerProps={{ "aria-label": `${label.title}: your table` }}
          value={value?.source.split(".").pop() ?? ""}
          onSelect={pick}
        >
          <Select.Item value="">{label.required ? "Choose a table…" : "None"}</Select.Item>
          {tables.map((t) => (
            <Select.Item key={t.table} value={t.table}>
              {t.table} · {t.rows.toLocaleString()} rows
            </Select.Item>
          ))}
        </Select>
      </div>
      {value && (
        <>
          {role === "edges" &&
            proposal?.roles.edges_derived_from &&
            value.source.endsWith(`.${proposal.roles.edges}`) && (
              <p className="text-xs text-text-muted">
                Derived from <span className="font-mono">{proposal.roles.edges_derived_from}</span>: each change links
                to the item it names.
              </p>
            )}
          {/* biome-ignore lint/a11y/useSemanticElements: a labelled grid of field rows; <fieldset> does not lay out as a CSS grid in every browser */}
          <div
            className="grid grid-cols-1 sm:grid-cols-[10rem_minmax(0,1fr)] gap-x-3 gap-y-1.5 items-center"
            role="group"
            aria-label={`${label.title} fields`}
          >
            {shown.map((f) => (
              <FieldRow
                key={f.key}
                spec={f}
                role={role}
                expr={value.columns[f.key]}
                cols={cols}
                colNames={colNames}
                confidence={
                  suggested[f.key] && JSON.stringify(suggested[f.key].from) === JSON.stringify(value.columns[f.key])
                    ? suggested[f.key].confidence
                    : undefined
                }
                onChange={(e) => setField(f.key, e)}
              />
            ))}
          </div>
          {(hidden > 0 || more) && (
            <Button
              type="empty"
              htmlType="button"
              iconLeft={more ? "chevron-down" : "chevron-right"}
              aria-expanded={more}
              onClick={() => setMore((v) => !v)}
            >
              {more ? "Fewer fields" : `${hidden} more field${hidden === 1 ? "" : "s"}`}
            </Button>
          )}
        </>
      )}
    </section>
  );
}

function FieldRow({
  spec,
  role,
  expr,
  cols,
  colNames,
  confidence,
  onChange,
}: {
  spec: FieldSpec;
  role: LogicalTable;
  expr: MapExpr | undefined;
  cols: { name: string; type: string }[];
  colNames: Set<string>;
  confidence?: "high" | "medium" | "low";
  onChange: (e: MapExpr | undefined) => void;
}) {
  const fid = `f-${role}-${spec.key}`;
  const labelEl = (
    <Label htmlFor={fid}>
      {spec.label}
      {spec.core && (
        <span aria-hidden="true" className="text-warn">
          {" "}
          *
        </span>
      )}
      {spec.core && <span className="sr-only"> (required)</span>}
    </Label>
  );
  const badge = confidence && (
    <Badge
      size="sm"
      state={confidence === "high" ? "success" : "neutral"}
      text={confidence === "high" ? "matched" : "guessed"}
    />
  );
  if (spec.kind === "ref") {
    const parts = refParts(expr);
    const repoValue = parts ? ("column" in parts.repo ? `col:${parts.repo.column}` : "fixed") : "";
    const numberValue = !parts
      ? ""
      : typeof parts.number === "string"
        ? `col:${parts.number}`
        : `digits:${parts.number.digits}`;
    const set = (p: { repo?: string; fixed?: string; number?: string }) => {
      const cur = parts ?? { repo: { fixed: "" }, number: "" };
      const repo =
        p.repo !== undefined
          ? p.repo.startsWith("col:")
            ? { column: p.repo.slice(4) }
            : { fixed: "fixed" in cur.repo ? cur.repo.fixed : "" }
          : p.fixed !== undefined
            ? { fixed: p.fixed }
            : cur.repo;
      const number =
        p.number === undefined
          ? cur.number
          : p.number.startsWith("digits:")
            ? { digits: p.number.slice(7) }
            : p.number.startsWith("col:")
              ? p.number.slice(4)
              : "";
      onChange(number ? refExpr({ repo, number }) : undefined);
    };
    return (
      <>
        {labelEl}
        <div className="flex flex-wrap items-center gap-1.5 min-w-0">
          {expr !== undefined && !parts ? (
            <>
              <span className="font-mono text-xs">{describeExpr(expr)}</span>
              <Button type="empty" htmlType="button" onClick={() => onChange(undefined)}>
                Change
              </Button>
            </>
          ) : (
            <>
              <Select
                id={fid}
                className="w-44"
                triggerProps={{ "aria-label": `${spec.label}: repository` }}
                value={repoValue}
                onSelect={(v) => set({ repo: v })}
              >
                <Select.Item value="fixed">one repository:</Select.Item>
                {cols.map((c) => (
                  <Select.Item key={c.name} value={`col:${c.name}`}>
                    {c.name}
                  </Select.Item>
                ))}
              </Select>
              {(!parts || "fixed" in parts.repo) && (
                <TextField
                  aria-label={`${spec.label}: fixed repository`}
                  className="w-40"
                  placeholder="Org/repo"
                  value={parts && "fixed" in parts.repo ? parts.repo.fixed : ""}
                  onChange={(v) => set({ fixed: v })}
                />
              )}
              <span aria-hidden="true" className="font-mono text-xs">
                #
              </span>
              <Select
                className="w-56"
                triggerProps={{ "aria-label": `${spec.label}: number` }}
                value={numberValue}
                onSelect={(v) => set({ number: v })}
              >
                <Select.Item value="">number column…</Select.Item>
                {cols.map((c) => (
                  <Select.Item key={c.name} value={`col:${c.name}`}>
                    {c.name} · {shortType(c.type)}
                  </Select.Item>
                ))}
                <Select.Group heading="The number at the end of a key ('HD-7' → 7)">
                  {cols
                    .filter((c) => /String/.test(c.type))
                    .map((c) => (
                      <Select.Item key={`d-${c.name}`} value={`digits:${c.name}`}>
                        number in {c.name}
                      </Select.Item>
                    ))}
                </Select.Group>
              </Select>
            </>
          )}
          {badge}
        </div>
      </>
    );
  }
  if (spec.kind === "type") {
    const cur =
      expr === undefined
        ? ""
        : typeof expr === "string"
          ? `col:${expr}`
          : "lit" in expr
            ? `lit:${String(expr.lit)}`
            : "expr";
    return (
      <>
        {labelEl}
        <div className="flex flex-wrap items-center gap-1.5">
          <Select
            id={fid}
            className="w-56"
            value={cur}
            onSelect={(v) => {
              if (v === "expr") return;
              onChange(v === "" ? undefined : v.startsWith("col:") ? v.slice(4) : { lit: v.slice(4) });
            }}
          >
            <Select.Item value="">choose…</Select.Item>
            <Select.Group heading="Always">
              {TYPE_VALUES.map((t) => (
                <Select.Item key={t} value={`lit:${t}`}>
                  {t}
                </Select.Item>
              ))}
            </Select.Group>
            <Select.Group heading="From a column">
              {cols.map((c) => (
                <Select.Item key={c.name} value={`col:${c.name}`}>
                  {c.name}
                </Select.Item>
              ))}
            </Select.Group>
            {cur === "expr" && <Select.Item value="expr">{describeExpr(expr)}</Select.Item>}
          </Select>
          {badge}
        </div>
      </>
    );
  }
  const custom = expr !== undefined && (typeof expr !== "string" || !colNames.has(expr));
  return (
    <>
      {labelEl}
      <div className="flex flex-wrap items-center gap-1.5 min-w-0">
        <Select
          id={fid}
          className="w-56"
          value={custom ? "__custom" : typeof expr === "string" ? expr : ""}
          onSelect={(v) => onChange(v === "" ? undefined : v === "__custom" ? expr : v)}
        >
          <Select.Item value="">{spec.core ? "choose a column…" : "Loom default"}</Select.Item>
          {cols.map((c) => (
            <Select.Item key={c.name} value={c.name}>
              {c.name} · {shortType(c.type)}
            </Select.Item>
          ))}
          {custom && <Select.Item value="__custom">{describeExpr(expr)}</Select.Item>}
        </Select>
        {badge}
        {spec.hint && <span className="text-xs text-text-muted min-w-0">{spec.hint}</span>}
      </div>
    </>
  );
}
