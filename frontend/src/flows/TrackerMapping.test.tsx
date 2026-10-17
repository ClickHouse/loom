/**
 * The Map step edits a mapping as data. A reference is built from a repository (a column,
 * or one fixed repository) and a number (a column, or the number at the end of a key);
 * switching tables keeps the fields the new table also has; an optional role can be left
 * out.
 */
import { cleanup, fireEvent, render, screen, within } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  describeExpr,
  mappingProblems,
  refExpr,
  refParts,
  type InspectedTable,
  type SourceMapping,
} from "../lib/trackerSources";
import { axeViolations, unlabeledControls } from "../test/axe";
import { MappingEditor } from "./TrackerMapping";

const ISSUES: InspectedTable = {
  table: "issues",
  engine: "MergeTree",
  rows: 3,
  columns: [
    { name: "repo", type: "String" },
    { name: "issue_id", type: "UInt32" },
    { name: "title", type: "String" },
    { name: "key", type: "String" },
    { name: "opened_at", type: "DateTime" },
  ],
};
const BUGS: InspectedTable = {
  table: "bugs",
  engine: "ReplacingMergeTree",
  rows: 9,
  columns: [
    { name: "title", type: "String" },
    { name: "filed", type: "DateTime" },
  ],
};
const PRS: InspectedTable = {
  table: "pull_requests",
  engine: "MergeTree",
  rows: 2,
  columns: [{ name: "pr_id", type: "UInt32" }],
};

function edit(mapping: SourceMapping) {
  const onChange = vi.fn();
  render(
    <main>
      <MappingEditor
        database="acme"
        tables={[ISSUES, BUGS, PRS]}
        mapping={mapping}
        proposal={null}
        onChange={onChange}
      />
    </main>,
  );
  return onChange;
}
/** Opens a Click UI Select by its trigger's name and picks the option with this text. */
function choose(trigger: RegExp | string, option: RegExp | string) {
  fireEvent.click(screen.getByRole("button", { name: trigger }));
  fireEvent.click(within(screen.getByRole("dialog")).getByText(option));
}
const ISSUES_MAP: SourceMapping = {
  version: 1,
  tables: {
    github_issues: {
      source: "acme.issues",
      final: false,
      columns: {
        source_id: { concat: ["repo", { lit: "#" }, "issue_id"] },
        number: "issue_id",
        title: "title",
        created_at: "opened_at",
      },
    },
  },
};

describe("the reference, as parts", () => {
  it("reads and writes both shapes a tracker has", () => {
    expect(refParts({ concat: ["repo", { lit: "#" }, "issue_id"] })).toEqual({
      repo: { column: "repo" },
      number: "issue_id",
    });
    expect(refParts({ concat: [{ lit: "Acme/w#" }, { digits: "key" }] })).toEqual({
      repo: { fixed: "Acme/w" },
      number: { digits: "key" },
    });
    expect(refParts("source_id")).toBeNull();
    expect(refExpr({ repo: { fixed: "Acme/w" }, number: "n" })).toEqual({ concat: [{ lit: "Acme/w#" }, "n"] });
    expect(describeExpr({ digits: "key" })).toMatch(/key/);
    expect(mappingProblems(ISSUES_MAP)).toEqual([]);
  });
});

describe("MappingEditor", () => {
  afterEach(cleanup);

  it("shows each role's table and fields, labelled and axe-clean", async () => {
    edit(ISSUES_MAP);
    expect(screen.getByRole("button", { name: "Items: your table" })).toHaveTextContent("issues · 3 rows");
    expect(screen.getByRole("button", { name: "Pull requests: your table" })).toHaveTextContent("None");
    expect(screen.getByRole("button", { name: /Reference: repository/ })).toHaveTextContent(/^repo$/);
    expect(screen.getByRole("button", { name: /Reference: number/ })).toHaveTextContent("issue_id · UInt32");
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("builds a reference from one fixed repository and the number in a key", () => {
    const onChange = edit(ISSUES_MAP);
    choose(/Reference: number/, "number in key");
    expect(onChange.mock.lastCall?.[0].tables.github_issues.columns.source_id).toEqual({
      concat: ["repo", { lit: "#" }, { digits: "key" }],
    });
    cleanup();
    const again = edit(ISSUES_MAP);
    choose(/Reference: repository/, "one repository:");
    expect(again.mock.lastCall?.[0].tables.github_issues.columns.source_id).toEqual({
      concat: [{ lit: "#" }, "issue_id"],
    });
  });

  it("switching tables keeps the fields the new table has, and sets FINAL by its engine", () => {
    const onChange = edit(ISSUES_MAP);
    choose("Items: your table", /^bugs/);
    const t = onChange.mock.lastCall?.[0].tables.github_issues;
    expect(t.source).toBe("acme.bugs");
    expect(t.final).toBe(true);
    expect(t.columns.title).toBe("title");
    expect(t.columns.created_at).toBeUndefined();
  });

  it("an optional role is added and left out by its table picker", () => {
    const onChange = edit(ISSUES_MAP);
    choose("Pull requests: your table", /^pull_requests/);
    expect(onChange.mock.lastCall?.[0].tables.github_prs.source).toBe("acme.pull_requests");
    cleanup();
    const both: SourceMapping = {
      version: 1,
      tables: { ...ISSUES_MAP.tables, github_prs: { source: "acme.pull_requests", final: false, columns: {} } },
    };
    const again = edit(both);
    choose("Pull requests: your table", "None");
    expect(again.mock.lastCall?.[0].tables.github_prs).toBeUndefined();
    expect(again.mock.lastCall?.[0].tables.github_issues).toBeDefined();
  });
});
