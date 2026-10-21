// A login link: the token a sign-in hands the dashboard in its address, and the page that
// sign-in started on. App's boot loads this only when the address carries a token, and the
// sign-in gate loads it with itself, so the shell every page boots carries none of it.

/** The session key for where a single sign-on started, `{ path, at }`: the identity
 *  provider sends the browser back to the root. */
export const SIGN_IN_FROM = "loom_sign_in_from_v1";
/** How long a sign-in's start stays worth returning to. */
const SIGN_IN_TTL_MS = 10 * 60 * 1000;

/** Remember the page a sign-in starts on, and when (the gate, as it sends the browser to
 *  the identity provider). From the bare root there is nothing to come back to. */
export function rememberSignInStart(): void {
  const path = window.location.pathname + window.location.search;
  try {
    if (path === "/") sessionStorage.removeItem(SIGN_IN_FROM);
    else sessionStorage.setItem(SIGN_IN_FROM, JSON.stringify({ path, at: Date.now() }));
  } catch {
    /* no session storage: the sign-in lands on the root */
  }
}

/** The page a sign-in started on, read and cleared: ten minutes old at most, a path on this
 *  origin without control characters. */
function takeSignInStart(): string | null {
  try {
    const raw = sessionStorage.getItem(SIGN_IN_FROM);
    sessionStorage.removeItem(SIGN_IN_FROM);
    const { path, at } = JSON.parse(raw ?? "{}") as { path?: unknown; at?: unknown };
    if (typeof path !== "string" || typeof at !== "number" || Date.now() - at > SIGN_IN_TTL_MS) return null;
    if (!path.startsWith("/") || /[\u0000-\u001f\u007f]/.test(path)) return null;
    const url = new URL(path, window.location.origin);
    return url.origin === window.location.origin ? url.pathname + url.search : null;
  } catch {
    return null;
  }
}

/** The token a login link carries in its `#token=` fragment, or null. A
 *  fragment never reaches the server or its logs; a `?token=` query does, so it
 *  is not accepted. Both are scrubbed from the address bar (and history) so a
 *  token never lingers in a copied link. Any token's arrival ends the sign-in it
 *  may come from; one that comes back to the bare root (`/`, or `/me` from a server
 *  that lands there) opens the page that sign-in started on. */
export function takeLinkToken(): string | null {
  const { hash, search, pathname } = window.location;
  const fromHash = hash.startsWith("#token=");
  const query = new URLSearchParams(search);
  if (!fromHash && !query.has("token")) return null;
  query.delete("token");
  const qs = query.toString();
  const from = takeSignInStart();
  const scrubbed = pathname + (qs ? `?${qs}` : "") + (fromHash ? "" : hash);
  try {
    history.replaceState(null, "", (fromHash && !qs && (pathname === "/" || pathname === "/me") && from) || scrubbed);
  } catch {
    // Boot never goes on with the token in the address.
    history.replaceState(null, "", scrubbed);
  }
  if (!fromHash) return null;
  try {
    return decodeURIComponent(hash.slice("#token=".length)) || null;
  } catch {
    return null;
  }
}
