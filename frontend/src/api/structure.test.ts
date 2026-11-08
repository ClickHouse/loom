import { afterEach, describe, expect, it, vi } from "vitest";

// The request bodies Settings → Structure sends, against the server's input models.
const post = vi.hoisted(() => vi.fn());
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), post }));
import { listOntology, listRecordSchemas, queryRecords, recordConflicts, registerOntologyTerm } from "./structure";
import type { OntologyEntry } from "./structure";

const id = { org: "acme", namespace: "team" };

afterEach(() => vi.clearAllMocks());

describe("structure client", () => {
  it("lists every ontology status in one call, pending first, without retired terms", async () => {
    post.mockResolvedValueOnce({
      entries: [
        { kind: "entity_type", name: "Customer", status: "accepted", description: "", cardinality: "" },
        { kind: "relation_verb", name: "works_at", status: "pending", description: "", cardinality: "one" },
        { kind: "entity_type", name: "Old", status: "tombstoned", description: "", cardinality: "" },
      ],
    });
    const entries = await listOntology(id);
    expect(post).toHaveBeenCalledWith("/v1/namespace.list_ontology", { ...id, status: null }, undefined);
    expect(entries.map((e) => e.name)).toEqual(["works_at", "Customer"]);
  });

  it("sends only the four fields a registration takes, even from a listed entry", async () => {
    post.mockResolvedValueOnce({ registered: true });
    const listed = {
      kind: "relation_verb",
      name: "works_at",
      description: "employer",
      cardinality: "one",
      status: "pending",
      created_by: "agent",
      updated_at: "2000-01-01 00:00:00",
    } as OntologyEntry;
    await registerOntologyTerm(id, listed);
    expect(post).toHaveBeenCalledWith("/v1/namespace.register_ontology", {
      ...id,
      kind: "relation_verb",
      name: "works_at",
      description: "employer",
      cardinality: "one",
      status: "accepted",
    });
  });

  it("lists proposed schemas and bounds the record reads", async () => {
    post.mockResolvedValue({ schemas: [], rows: [], truncated: false });
    await listRecordSchemas(id);
    await queryRecords(id, "invoice");
    await recordConflicts(id, "invoice");
    expect(post.mock.calls.map((c) => [c[0], c[1]])).toEqual([
      ["/v1/memory_schema.list", { ...id, include_proposed: true }],
      ["/v1/memory.record.query", { ...id, schema_name: "invoice", limit: 100 }],
      ["/v1/memory.record.conflicts", { ...id, schema_name: "invoice", limit: 100 }],
    ]);
  });
});
