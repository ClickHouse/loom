// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";
import type { GrantRole, RequestOpts } from "../api";
import { COLLECTIONS_PAGE } from "../lib/collections";

// ===== Collections =====

/** Mirrors `server/identity/api/models_collections.py::_CollectionEntry`. */
export interface CollectionEntry {
  namespace: string;
  /** The collection it sits in and inherits grants from; "" for a top-level one. */
  parent: string;
  /** A user's own `@me` collection: it takes no grant and is erased with its user. */
  personal: boolean;
  /** The caller's role there. */
  role: GrantRole;
  /** What it holds: its directory description; "" for none (and from a server a release behind). */
  description?: string;
  /** What it is called on screen; "" for none (then its name is shown). */
  title?: string;
}

export interface CollectionListPage {
  collections: CollectionEntry[];
  next_cursor: string | null;
}

/** The collections the caller can read, by name, one page at a time. */
export async function listCollections(
  org: string,
  opts: { cursor?: string | null; limit?: number } & RequestOpts = {},
): Promise<CollectionListPage> {
  const body: Record<string, unknown> = { org, limit: opts.limit ?? COLLECTIONS_PAGE };
  if (opts.cursor) body["cursor"] = opts.cursor;
  const r = await post<CollectionListPage>("/v1/collection.list", body, opts.signal);
  return { collections: r.collections ?? [], next_cursor: r.next_cursor ?? null };
}

export interface CollectionCreated {
  namespace: string;
  parent: string;
  /** False when the collection already existed: the caller's own by that name, or, with
   *  `deriveName`, the one the title already names under that parent. */
  created: boolean;
  /** Its title; "" for none (and from a server a release behind). */
  title?: string;
}

/** POST /v1/collection.create. With `deriveName` the server derives the name from the
 *  title and `namespace` is only what a server a release behind creates. */
export async function createCollection(
  org: string,
  namespace: string,
  opts: { parent?: string; description?: string; title?: string; deriveName?: boolean } = {},
): Promise<CollectionCreated> {
  return post("/v1/collection.create", {
    org,
    namespace,
    parent: opts.parent ?? "",
    description: opts.description ?? "",
    title: opts.title ?? "",
    derive_name: opts.deriveName ?? false,
  });
}

export interface CollectionDeleteResult {
  namespace: string;
  /** False when it was already deleted. */
  deleted: boolean;
  /** The erasure of what it held; `erasure.status` follows it. */
  erasure_id: string;
  /** `completed`, or `failed` while the recovery worker retries it. */
  erasure_status: string;
}

/** Delete a collection with what it holds: a purging erasure of its namespace, then it
 *  leaves the directory and the tree, and its children become top-level. */
export async function deleteCollection(org: string, namespace: string): Promise<CollectionDeleteResult> {
  return post("/v1/collection.delete", { org, namespace });
}

/** POST /v1/collection.move (ADMIN): put a collection under `parent`, or "" to make it
 *  top-level. Its grants then follow the new parent's. */
export async function moveCollection(
  org: string,
  namespace: string,
  parent: string,
): Promise<{ namespace: string; parent: string; moved: boolean }> {
  return post("/v1/collection.move", { org, namespace, parent });
}
