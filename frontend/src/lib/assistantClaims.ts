// The reason a token's own claims keep the assistant from answering, in its own module so
// the header can read it before the assistant's code loads (the boot chunk's budget).
import { isAuthDisabled, storedToken } from "../api";
import type { AssistantReason } from "./assistantLayout";
import { tokenPayload } from "./boot";

/** The reason the token's own claims give, read without verification (the route's refusal
 *  stays the truth): auth off, an org-wide token, a read scope narrower than the namespace.
 *  A token that expires soon is the route's to say (a first-party session is refreshed). */
export function claimReason(token: string | null = storedToken()): AssistantReason | null {
  if (isAuthDisabled()) return { kind: "auth_off" };
  const c = tokenPayload(token);
  if (!c) return null;
  if (c.org === "*") return { kind: "org_wide" };
  const pinned = typeof c.read_user_id === "string" ? c.read_user_id.trim() : "";
  if (c.read_scope === "own_session") return { kind: "narrowed", scope: "one session" };
  if (c.read_scope === "own_user" || pinned) return { kind: "narrowed", scope: pinned ? `user ${pinned}` : "one user" };
  return null;
}
