// How a session ends: the note a reload's sign-in gate reads, and whoami asked again while
// the replica's keys load. App's boot and the cross-tab handler (./tokenElsewhere) share it.
import { clearToken, isAuthDisabled, storedToken, whoami, type ApiError, type WhoAmI } from "../api";
import { claimsFromToken } from "./boot";
import { useStore, type GateReason } from "./state";

// This tab's session ended on a refused token: a reload's sign-in gate says so, and when the
// token was a sign-in session's (`sid`) starts the sign-in by itself, though it is gone.
const SESSION_ENDED = "loom_session_ended_v1";

/** Note why this tab's session ended (`reason` given), or read the note and forget it: the
 *  gate's reason, or null. */
export function sessionEndedNote(reason?: GateReason): GateReason {
  try {
    const was = sessionStorage.getItem(SESSION_ENDED) as GateReason;
    if (reason) sessionStorage.setItem(SESSION_ENDED, reason);
    else sessionStorage.removeItem(SESSION_ENDED);
    return was;
  } catch {
    return null; // no session storage: a reload shows the first-visit gate
  }
}

/** The gate's reason for a lost token: only a sign-in session's resumes by itself. */
export const lostReason = (token: string | null): GateReason => (claimsFromToken(token)?.session ? "expired" : "ended");

/** The session ended (the server refused the token, or another tab dropped it). While the
 *  app boots there is no page to keep, and boot, which knows whether the token came by a
 *  sign-in link, shows the gate and says why once whoami answers. Mid-session a card says
 *  so over the page, which stays as it is. */
export function endSession(lost = storedToken()): void {
  // `lost` is read on the call, before the token is cleared.
  clearToken();
  // A single-user install has no sign-in: its next call goes without the stale token.
  if (isAuthDisabled()) return;
  sessionEndedNote(lostReason(lost));
  const s = useStore.getState();
  if (s.whoami) s.setSessionExpired(true);
}

/** whoami, asked again while the replica answers 503 (its signing keys still loading, the
 *  token may be fine): up to five more times, a second apart (the server's own Retry-After
 *  is 5 s), before boot gives up. With `asToken`, about that token instead of the stored one,
 *  which stays as it is. */
export async function whoamiWhenReady(asToken?: string): Promise<WhoAmI> {
  // A token stored meanwhile is not the one asked about; with asToken the stored one plays no part.
  const asked = asToken ? null : storedToken();
  const changed = () => !asToken && storedToken() !== asked;
  for (let attempt = 1; ; attempt++) {
    try {
      return await whoami(asToken);
    } catch (e) {
      // Only a 503 (an ApiError's status; any other error has none) is asked again.
      if (!changed() && (attempt >= 6 || (e as ApiError).status !== 503)) throw e;
    }
    if (!changed()) await new Promise((r) => setTimeout(r, 1000));
    if (changed()) return { authenticated: false };
  }
}
