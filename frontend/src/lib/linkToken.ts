import { SIGN_IN_FROM } from "./boot";

/** How long a sign-in's start stays worth returning to. */
const SIGN_IN_TTL_MS = 10 * 60 * 1000;

/** Restore a recent same-origin sign-in path after taking the login token. */
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
