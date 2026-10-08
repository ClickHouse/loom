import { describe, expect, it } from "vitest";
import {
  emptyFilters,
  isProvenance,
  matchEntities,
  type Provenance,
  visibleNodes,
  type WalkModel,
  type XNode,
} from "./model";

const entities = ["clickhouse", "loom", "hosted clickhouse", "click", "merge tree", "clickpipes"].map((value, i) => ({
  value,
  count: 10 - i,
}));

describe("matchEntities", () => {
  it("ranks prefix matches before substring matches, each in taxonomy order", () => {
    expect(matchEntities(entities, "click", 10).map((e) => e.value)).toEqual([
      "clickhouse",
      "click",
      "clickpipes",
      "hosted clickhouse",
    ]);
  });

  it("keeps at most `cap` matches, prefix ones first", () => {
    expect(matchEntities(entities, "click", 2).map((e) => e.value)).toEqual(["clickhouse", "click"]);
  });

  it("an empty query or no match gives nothing", () => {
    expect(matchEntities(entities, "", 8)).toEqual([]);
    expect(matchEntities(entities, "zzz", 8)).toEqual([]);
  });
});

function walkWith(via_provenance: string): WalkModel {
  const node: XNode = {
    entity: "loom",
    hop: 1,
    via_co_occur: 0,
    via_relation: "uses",
    via_direction: "out",
    via_provenance,
    valid_from: "",
    valid_to: "",
    community_id: null,
    path: ["clickhouse", "loom"],
    parent: "clickhouse",
    origin: "walk",
  };
  return { seed: "clickhouse", seedCommunityId: null, nodes: new Map([[node.entity, node]]), truncated: false };
}

const extractedOnly = () => ({ ...emptyFilters(), provenance: new Set<Provenance>(["extracted"]) });

describe("provenance chips", () => {
  it("isProvenance accepts the three tiers and nothing else", () => {
    expect(["extracted", "inferred", "facet"].every(isProvenance)).toBe(true);
    expect(isProvenance("")).toBe(false);
    expect(isProvenance("asserted")).toBe(false);
  });

  it("an unknown provenance fails an active filter and passes an empty one", () => {
    const m = walkWith("asserted");
    expect(visibleNodes(m, extractedOnly(), new Set(), new Set()).nodes).toEqual([]);
    expect(visibleNodes(m, emptyFilters(), new Set(), new Set()).nodes.map((n) => n.entity)).toEqual(["loom"]);
  });

  it("a known provenance passes the filter that names it", () => {
    const visible = visibleNodes(walkWith("extracted"), extractedOnly(), new Set(), new Set());
    expect(visible.nodes.map((n) => n.entity)).toEqual(["loom"]);
  });
});
