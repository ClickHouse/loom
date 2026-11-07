// A namespace's declared structure: typed-record schemas and their records, the profile
// schema, the graph's ontology and the category registry.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

type Scope = Pick<Identity, "org" | "namespace">;
const scope = (id: Scope) => ({ org: id.org, namespace: id.namespace });

// ===== Record schemas =====

export type SchemaFieldType = "string" | "number" | "integer" | "boolean";

export interface SchemaField {
  name: string;
  type: SchemaFieldType;
  required: boolean;
  /** Part of the record's identity: a newer record with the same identity supersedes it. */
  identity: boolean;
}

/** Mirrors `server/memory/api/models_records.py::MemorySchemaSummary`. */
export interface RecordSchema {
  schema_id: string;
  name: string;
  description: string;
  /** `active`, `proposed` until an admin decides, or `rejected`. */
  status: string;
  fields: SchemaField[];
}

export async function listRecordSchemas(id: Scope, opts: RequestOpts = {}): Promise<{ schemas: RecordSchema[] }> {
  return post("/v1/memory_schema.list", { ...scope(id), include_proposed: true }, opts.signal);
}

export async function registerRecordSchema(
  id: Scope,
  name: string,
  description: string,
  fields: SchemaField[],
): Promise<{ name: string; status: string }> {
  return post("/v1/memory_schema.register", { ...scope(id), name, description, fields });
}

export async function decideRecordSchema(
  id: Scope,
  name: string,
  decision: "approve" | "reject",
): Promise<{ status: string; updated: boolean }> {
  return post("/v1/memory_schema.approve", { ...scope(id), name, decision });
}

// ===== Typed records =====

/** Mirrors `server/memory/api/models_records.py::MemoryRecordRow`. */
export interface TypedRecord {
  record_id: string;
  memory_key: string;
  record: Record<string, unknown>;
  status: string;
  valid_at: string | null;
  valid_to: string | null;
}

export async function queryRecords(
  id: Scope,
  schemaName: string,
  opts: RequestOpts = {},
): Promise<{ rows: TypedRecord[]; truncated: boolean }> {
  return post("/v1/memory.record.query", { ...scope(id), schema_name: schemaName, limit: RECORD_LIMIT }, opts.signal);
}

export const RECORD_LIMIT = 100;

export async function recordHistory(
  id: Scope,
  recordId: string,
  opts: RequestOpts = {},
): Promise<{ rows: TypedRecord[] }> {
  return post("/v1/memory.record.history", { ...scope(id), record_id: recordId }, opts.signal);
}

/** Mirrors `server/memory/api/models_records.py::MemoryRecordConflictRow`. */
export interface RecordConflict {
  memory_key: string;
  field_name: string;
  prior_value: unknown;
  incoming_value: unknown;
  /** The record the newer one superseded; its history holds what it said. */
  prior_record_id: string;
  incoming_record_id: string;
  detected_at: string | null;
}

export async function recordConflicts(
  id: Scope,
  schemaName: string,
  opts: RequestOpts = {},
): Promise<{ rows: RecordConflict[]; truncated: boolean }> {
  return post(
    "/v1/memory.record.conflicts",
    { ...scope(id), schema_name: schemaName, limit: RECORD_LIMIT },
    opts.signal,
  );
}

// ===== Profile schema =====

export interface ProfileSchema {
  found: boolean;
  json_schema: Record<string, unknown>;
  updated_by: string;
  updated_at: string | null;
}

export async function getProfileSchema(id: Scope, opts: RequestOpts = {}): Promise<ProfileSchema> {
  return post("/v1/profile_schema.get", scope(id), opts.signal);
}

export async function setProfileSchema(id: Scope, jsonSchema: Record<string, unknown>): Promise<ProfileSchema> {
  return post("/v1/profile_schema.set", { ...scope(id), json_schema: jsonSchema });
}

export async function clearProfileSchema(id: Scope): Promise<ProfileSchema> {
  return post("/v1/profile_schema.clear", scope(id));
}

// ===== Ontology =====

export type OntologyKind = "entity_type" | "relation_verb" | "attribute";

export interface OntologyEntry {
  kind: OntologyKind;
  name: string;
  /** `accepted`, or `pending` until an admin accepts it. */
  status: string;
  description: string;
  /** A relation's or attribute's `one` or `many`; "" lets the seed list decide. */
  cardinality: string;
}

/** The accepted and pending terms, pending first. */
export async function listOntology(id: Scope, opts: RequestOpts = {}): Promise<OntologyEntry[]> {
  const r = await post<{ entries: OntologyEntry[] }>(
    "/v1/namespace.list_ontology",
    { ...scope(id), status: null },
    opts.signal,
  );
  const live = r.entries.filter((e) => e.status !== "tombstoned");
  return [...live.filter((e) => e.status === "pending"), ...live.filter((e) => e.status !== "pending")];
}

/** POST /v1/namespace.register_ontology: a new term, or a pending one accepted. A
 *  registration replaces the whole entry, so an accepted term keeps its own fields. */
export async function registerOntologyTerm(
  id: Scope,
  term: Pick<OntologyEntry, "kind" | "name" | "description" | "cardinality">,
): Promise<{ registered: boolean }> {
  return post("/v1/namespace.register_ontology", { ...scope(id), ...term, status: "accepted" });
}

// ===== Categories =====

export interface Category {
  category_id: string;
  name: string;
  description: string;
  item_count: number;
}

export async function listCategories(id: Scope, opts: RequestOpts = {}): Promise<Category[]> {
  const r = await post<{ categories: Category[] }>("/v1/namespace.list_categories", scope(id), opts.signal);
  return r.categories ?? [];
}

export async function registerCategory(id: Scope, name: string, description: string): Promise<{ category_id: string }> {
  return post("/v1/namespace.register_category", { ...scope(id), name, description });
}
