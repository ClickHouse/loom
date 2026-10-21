// The page a single sign-on started on, returned to when the sign-in's token comes back to
// the root. App's boot loads this after it has taken and scrubbed the token
// (`takeLinkToken` in lib/boot.ts), and goes on without it when it cannot load: the
// return is a convenience, never part of signing in.
import { SIGN_IN_FROM } from "./boot";

/** How long a sign-in's start stays worth returning to. */
const SIGN_IN_TTL_MS = 10 * 60 * 1000;

/** End the sign-in a token may come from: read and clear where it started, and when the
 *  token came back to the root, open that page again — ten minutes old at most, a path on
 *  this origin without control characters. */
export function returnToSignInStart(atRoot: boolean): void {
  try {
    const raw = sessionStorage.getItem(SIGN_IN_FROM);
    sessionStorage.removeItem(SIGN_IN_FROM);
    const { path, at } = JSON.parse(raw ?? "{}") as { path?: unknown; at?: unknown };
    if (!atRoot || typeof path !== "string" || typeof at !== "number" || Date.now() - at > SIGN_IN_TTL_MS) return;
    if (!path.startsWith("/") || /[\u0000-\u001f\u007f]/.test(path)) return;
    const url = new URL(path, window.location.origin);
    if (url.origin === window.location.origin) history.replaceState(null, "", url.pathname + url.search);
  } catch {
    /* the address stays where the token came back */
  }
}
