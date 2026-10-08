import { describe, expect, it } from "vitest";
import type { CodeRepoSummary } from "../api";
import { stageFlips, stageLabel } from "./codeStage";

// The card's stage pill says how far along a first index is — files and an ETA while
// indexing, the embedding percentage after, nothing at ready — and the page announces the
// flip to ready or error between polls.

const row = (over: Partial<CodeRepoSummary>): CodeRepoSummary => ({
  org: "o",
  namespace: "code-widgets",
  repo: "acme/widgets",
  branch: "main",
  git_head: "",
  clone_head: "",
  fast_tier_lag: -1,
  scip_lag: -1,
  scip_py_lag: -1,
  indexed_at: "",
  last_refresh_at: "",
  counts: {},
  ...over,
});

describe("stageLabel", () => {
  it("indexing shows the file counter and the worker's ETA", () => {
    const p = stageLabel(
      row({
        stage: "indexing",
        stage_detail: JSON.stringify({ done: 1200, total: 52000, files_per_s: 7.1, eta: "~38 min left" }),
      }),
    );
    expect(p?.label).toBe("indexing 1,200 of 52,000 · ~38 min left");
    expect(p?.tone).toBe("warn");
    expect(p?.title).toContain("7.1 files/s");
  });

  it("indexing without a rate has no ETA", () => {
    const p = stageLabel(row({ stage: "indexing", stage_detail: JSON.stringify({ done: 0, total: 52000 }) }));
    expect(p?.label).toBe("indexing 0 of 52,000");
  });

  it("embedding shows the row's coverage as a whole percent", () => {
    const p = stageLabel(
      row({ stage: "embedding", chunks_total: 1000, chunks_embedded: 413, embedding_coverage: 0.413 }),
    );
    expect(p?.label).toBe("embedding 41%");
    expect(p?.tone).toBe("warn");
    expect(p?.title).toContain("lexical-only");
    expect(p?.title).not.toContain("95%");
  });

  it("embedding falls back to the detail the worker wrote", () => {
    const p = stageLabel(
      row({ stage: "embedding", stage_detail: JSON.stringify({ chunks: 100, embedded: 20, embedding_coverage: 0.2 }) }),
    );
    expect(p?.label).toBe("embedding 20%");
  });

  it("ready and an operator-indexed entry show no stage pill", () => {
    expect(stageLabel(row({ stage: "ready" }))).toBeNull();
    expect(stageLabel(row({}))).toBeNull();
  });
  it("shows the embedding pill on an operator-indexed namespace whose vector leg is still filling", () => {
    const p = stageLabel(
      row({ stage: "", chunks_total: 1000, chunks_embedded: 413, embedding_coverage: 0.413, searchable: false }),
    );
    expect(p?.label).toBe("embedding 41%");
    expect(
      stageLabel(
        row({ stage: "ready", chunks_total: 1000, chunks_embedded: 990, embedding_coverage: 0.99, searchable: true }),
      ),
    ).toBeNull();
  });

  it("follows the server's verdict, not a copied threshold", () => {
    // An operator's 0.8 threshold: 85% is searchable, so no pill.
    expect(
      stageLabel(row({ chunks_total: 1000, chunks_embedded: 850, embedding_coverage: 0.85, searchable: true })),
    ).toBeNull();
    // A server that says not yet searchable at 97% still gets the pill.
    expect(
      stageLabel(row({ chunks_total: 1000, chunks_embedded: 970, embedding_coverage: 0.97, searchable: false }))?.label,
    ).toBe("embedding 97%");
  });

  it("error carries the reason", () => {
    const p = stageLabel(
      row({ stage: "error", stage_detail: JSON.stringify({ reason: "clone timed out; retrying next pass" }) }),
    );
    expect(p?.label).toBe("error");
    expect(p?.tone).toBe("bad");
    expect(p?.title).toBe("clone timed out; retrying next pass");
  });

  it("reads JSON that is not an object as an empty detail", () => {
    for (const stage of ["indexing", "embedding", "error"] as const) {
      const empty = stageLabel(row({ stage, stage_detail: "{}" }));
      for (const stage_detail of ["null", "[]", "3"]) {
        const p = stageLabel(row({ stage, stage_detail }));
        expect({ label: p?.label, tone: p?.tone }).toEqual({ label: empty?.label, tone: empty?.tone });
      }
    }
  });
});

describe("stageFlips", () => {
  it("announces embedding → ready and indexing → error, once", () => {
    const prev = [
      row({ stage: "embedding" }),
      row({ namespace: "code-gadgets", repo: "acme/gadgets", stage: "indexing" }),
    ];
    const next = [
      row({ stage: "ready" }),
      row({
        namespace: "code-gadgets",
        repo: "acme/gadgets",
        stage: "error",
        stage_detail: JSON.stringify({ reason: "git clone failed" }),
      }),
    ];
    expect(stageFlips(prev, next)).toEqual([
      { namespace: "code-widgets", repo: "acme/widgets", branch: "main", to: "ready", reason: "" },
      { namespace: "code-gadgets", repo: "acme/gadgets", branch: "main", to: "error", reason: "git clone failed" },
    ]);
    expect(stageFlips(next, next)).toEqual([]);
  });

  it("announces an error whose detail is not an object with no reason", () => {
    const next = [row({ stage: "error", stage_detail: "null" })];
    expect(stageFlips([row({ stage: "indexing" })], next)).toEqual([
      { namespace: "code-widgets", repo: "acme/widgets", branch: "main", to: "error", reason: "" },
    ]);
  });

  it("is silent on the first poll and for entries it has not seen", () => {
    expect(stageFlips(null, [row({ stage: "ready" })])).toEqual([]);
    expect(stageFlips([], [row({ stage: "ready" })])).toEqual([]);
  });

  it("does not announce a ready entry that was already ready or never built here", () => {
    expect(stageFlips([row({ stage: "" })], [row({ stage: "ready" })])).toEqual([]);
    expect(stageFlips([row({ stage: "error" })], [row({ stage: "error" })])).toEqual([]);
  });
});
