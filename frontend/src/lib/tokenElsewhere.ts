// Another tab's sign-in, sign-out or refused token, heard through the storage event. It
// ships in the session card's chunk (which App fetches as it loads), not the shell's.
import { ownsToken, signedOut, storedToken, type WhoAmI } from "../api";
import { reloadPage } from "./navigation";
import { endSession, sessionEndedNote, whoamiWhenReady } from "./session";
import { invalidateShared, sharedMine } from "./sharedReads";
import { useStore } from "./state";

let recheck: ReturnType<typeof setTimeout> | undefined;

/** Another tab of this origin stored a token (a sign-in there) or dropped it. Dropped by a
 *  sign-out: this tab reloads to a plain sign-in. Dropped by a refusal: this tab's session
 *  ended too. Stored: once the server confirms it is the same person, this page carries on
 *  with their identity and roles read again; anyone else gets a fresh page, so nothing the
 *  last person read stays on screen. `lost`: the token stored before, which a session that
 *  ends here takes its gate's reason from (the stored one is already gone or replaced). */
export async function tokenChangedElsewhere(token: string | null, lost: string | null): Promise<void> {
  clearTimeout(recheck); // this change decides
  const s = useStore.getState;
  const before = s().whoami;
  if (!before) return;
  if (token === null) {
    if (signedOut()) reloadPage();
    else if (!s().sessionExpired) endSession(lost);
    return;
  }
  // A token naming someone else is a fresh page at once: until a reload, every call here
  // would carry it under the last person's page.
  if (!before.subject || !ownsToken(token)) {
    reloadPage();
    return;
  }
  let now: WhoAmI;
  try {
    now = await whoamiWhenReady();
  } catch {
    // Unconfirmed: a healthy page stays as it is; an ended one keeps its card and asks
    // again shortly, while that token is still the stored one.
    if (s().sessionExpired)
      recheck = setTimeout(() => storedToken() === token && void tokenChangedElsewhere(token, lost), 30_000);
    return;
  }
  // Another change landed while whoami answered: that one decides.
  if (storedToken() !== token) return;
  // A token the server refuses is nobody's: this session ended, and the page stays. One it
  // accepts is the person its claims named, already checked above.
  if (!now.authenticated) {
    if (!s().sessionExpired) endSession(lost);
    return;
  }
  // The card going away is the sign that the session is back.
  useStore.setState({ whoami: now, sessionExpired: false });
  sessionEndedNote();
  try {
    invalidateShared();
    // The org boot read them for (`effectiveOrg`).
    s().setMyNamespaces(await sharedMine(now.org || "dev"));
  } catch {
    /* the roles read at boot stay; a reload reads them again */
  }
}
