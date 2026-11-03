import { tokenPayload } from "../../lib/boot";

/** The read scope a token's claims pin it to. */
export interface TokenReadScope {
  /** `own_session`, or `own_user` from that claim or from a `read_user_id` alone (the
   *  server narrows both the same way); null for a namespace-wide token. An end-user token
   *  is pinned by its issuer, which the claims do not name, so it reads null here. */
  readScope: "own_user" | "own_session" | null;
  /** The user `read_user_id` pins the token to; null when the claims name none. */
  readUserId: string | null;
}

/** The token's read scope, read without verification: it gates what the page offers, never
 *  what the server answers. Null for a missing or unparseable token. */
export function tokenReadScope(jwt: string | null | undefined): TokenReadScope | null {
  const json = tokenPayload(jwt);
  if (!json) return null;
  // Trimmed, as the server reads it: a blank pin pins nothing.
  const pinned = typeof json.read_user_id === "string" ? json.read_user_id.trim() : "";
  const readScope =
    json.read_scope === "own_session" ? "own_session" : json.read_scope === "own_user" || pinned ? "own_user" : null;
  return { readScope, readUserId: pinned || null };
}
