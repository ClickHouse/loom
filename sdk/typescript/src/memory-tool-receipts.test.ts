import { describe, expect, it } from "vitest";
import { Agent } from "./agent.js";
import { LoomClient } from "./client.js";
import type { MemorySearchOutput } from "./generated/ops.js";
import { Transport } from "./transport.js";

function tools(reply: unknown, maxResultChars = 8000) {
  const fetch = async () => new Response(JSON.stringify(reply), { status: 200 });
  const agent = new Agent(
    "acme",
    "support",
    "bot",
    new LoomClient(new Transport({ baseUrl: "http://x", fetch, logLevel: "off" })),
  );
  return agent.tools({}, { maxResultChars });
}

describe("memory tool evidence", () => {
  it.each([true, false])("separates governed authority from ordinary risk: %s", async (withGoverned) => {
    const ordinary = {
      memory_key: "ordinary",
      memory_type: "semantic" as const,
      content_excerpt: "Weak ordinary evidence",
      distance: 0.8,
      similarity: 0.2,
      below_threshold: true,
    };
    const definition = {
      memory_key: "definition/retry",
      memory_type: "semantic" as const,
      content_excerpt: "Retries require an idempotency key.",
      distance: 0.9,
      similarity: 0.1,
      governed: true,
    };
    const reply = {
      results: withGoverned ? [definition, ordinary] : [ordinary],
      embedding_model: "fixture",
      term_leg: "complete",
      abstain: !withGoverned,
      retrieval_risk: "abstain",
      retrieval_confidence: 0.0,
    } satisfies MemorySearchOutput;
    const out = JSON.parse(await tools(reply).execute("loom_recall", { query: "retry policy" }));
    expect(out).toMatchObject({
      term_leg: "complete",
      abstain: !withGoverned,
      retrieval_risk: "abstain",
      retrieval_confidence: 0.0,
    });
    if (withGoverned) expect(out.memories[0]).toMatchObject({ governed: true, similarity: 0.1 });
    expect(out.memories.at(-1).governed ?? false).toBe(false);
    expect(out.memories.at(-1).below_threshold).toBe(true);
  });

  it.each([
    [{ memory_id: "new", content_sha256: "abc", status: "active" }, true],
    [
      {
        memory_id: "canonical-id",
        content_sha256: "abc",
        noop: true,
        merged_into_key: "canonical",
        conflict_verdict: "merge",
      },
      false,
    ],
    [{ memory_id: "dup", status: "duplicate", duplicate_of: "canonical-id" }, true],
    [{ dry_run: true, noop: true, conflict_verdict: "clear" }, false],
    [
      {
        memory_id: "conflict",
        conflict_verdict: "conflict",
        peers: [{ memory_key: "other", excerpt: "Conflicting claim" }],
        next: "memory.update",
      },
      true,
    ],
  ] as const)("preserves the write receipt %j", async (fields, stored) => {
    const reply = { version: "dry_run" in fields ? 0 : 3, memory_key: "claim", namespace: "support", ...fields };
    const out = JSON.parse(await tools(reply).execute("loom_remember", { memory_key: "claim", value: "A fact" }));
    expect(out).toEqual({ ...reply, stored, verdict: "conflict_verdict" in fields ? fields.conflict_verdict : "" });
  });

  it.each(["fresh", "changed", "unresolved", "missing"])(
    "preserves %s code and provenance state",
    async (code_state) => {
      const metadata = {
        code_state,
        status: "conflicted",
        source_class: "external",
        source_resource_id: "src/reader.py",
        verified: false,
        valid_at: "2025-01-01T00:00:00Z",
        version: 3,
        namespace: "support",
      };
      const out = JSON.parse(
        await tools({
          abstain: false,
          results: [{ memory_key: "claim", content_excerpt: "A code claim", similarity: 0.9, ...metadata }],
        }).execute("loom_recall", { query: "reader" }),
      );
      expect(out.abstain).toBe(false);
      expect(out.memories[0]).toMatchObject(metadata);
    },
  );

  it("retains abstention even when weak results are served", async () => {
    const out = JSON.parse(
      await tools({
        abstain: true,
        abstain_code: "below_threshold",
        abstain_reason: "Weak evidence",
        pending_extraction: 2,
        results: [{ memory_key: "weak", content_excerpt: "Weak claim", similarity: 0.2, below_threshold: true }],
      }).execute("loom_recall", { query: "claim" }),
    );
    expect(out).toMatchObject({ abstain: true, abstain_code: "below_threshold", pending_extraction: 2 });
    expect(out.memories[0].below_threshold).toBe(true);
  });

  it("drops an oversized whole memory without slicing its trust metadata", async () => {
    const text = await tools(
      {
        abstain: false,
        results: [
          {
            memory_key: "large",
            content_excerpt: "x".repeat(2000),
            similarity: 0.9,
            code_state: "changed",
            verified: false,
          },
        ],
      },
      200,
    ).execute("loom_recall", { query: "claim" });
    expect(text.length).toBeLessThanOrEqual(200);
    expect(JSON.parse(text)).toEqual({ abstain: false, memories: [], omitted: 1 });
  });

  it("discloses an oversized receipt without claiming storage", async () => {
    const text = await tools({ version: 3, noop: true, merged_into_key: "x".repeat(2000) }, 100).execute(
      "loom_remember",
      { memory_key: "claim", value: "A fact" },
    );
    expect(text.length).toBeLessThanOrEqual(100);
    expect(JSON.parse(text)).toEqual({ insufficient: true, metadata_omitted: true, reason: "max_result_chars" });
  });

  it.each(["loom_remember", "loom_recall"])("rejects an unreportable budget before %s sends", async (name) => {
    let sent = false;
    const fetch = async () => {
      sent = true;
      return new Response("{}", { status: 200 });
    };
    const agent = new Agent(
      "acme",
      "support",
      "bot",
      new LoomClient(new Transport({ baseUrl: "http://x", fetch, logLevel: "off" })),
    );
    await expect(
      agent.tools({}, { maxResultChars: 20 }).execute(name, { memory_key: "claim", value: "A fact", query: "claim" }),
    ).rejects.toThrow(/too small to report memory result omissions/);
    expect(sent).toBe(false);
  });
});
