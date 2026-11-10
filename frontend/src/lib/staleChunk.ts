// A tab opened before a redeploy still names the old build's view chunks.
// The first visit to a view it had not loaded yet asks for a hashed file
// the new image no longer serves (404), the lazy import rejects, and the
// root error boundary shows "Something went wrong" — once; a reload fetches
// the new index and the page works. The ReloadBar only notices a new build on its
// 5-minute visible poll, so a tab woken up and clicked straight away hits the missing
// chunk first.
//
// Vite fires `vite:preloadError` on window when a dynamic import fails.
// Reload once instead of crashing; a second failure soon after the reload
// is not a stale tab, so it falls through to the error boundary.

const KEY = "loom_stale_chunk_reload_at";
/** A reload this recent means the chunk is missing from the current build too. */
export const STALE_CHUNK_RETRY_WINDOW_MS = 30_000;

export function installStaleChunkReload(
  win: Pick<Window, "addEventListener"> & { location: Pick<Location, "reload"> } = window,
  storage: Pick<Storage, "getItem" | "setItem"> | null = safeSessionStorage(),
  now: () => number = Date.now,
): void {
  win.addEventListener("vite:preloadError", (event: Event) => {
    // The error wording's chunk has its own fallback (Toast.tsx toastError): a reload for it
    // would lose the page, and the fallback toast with it.
    if (/errorWords/.test(preloadErrorText(event))) return;
    let last = 0;
    try {
      last = Number(storage?.getItem(KEY) ?? 0) || 0;
    } catch {
      /* storage blocked */
    }
    if (now() - last < STALE_CHUNK_RETRY_WINDOW_MS) return;
    try {
      storage?.setItem(KEY, String(now()));
    } catch {
      /* storage blocked */
    }
    event.preventDefault();
    win.location.reload();
  });
}

function safeSessionStorage(): Storage | null {
  try {
    return typeof sessionStorage !== "undefined" ? sessionStorage : null;
  } catch {
    return null;
  }
}

/** The failed import's own words, which name the chunk's file. */
function preloadErrorText(event: Event): string {
  const payload = (event as Event & { payload?: unknown }).payload;
  return payload instanceof Error ? payload.message : String(payload ?? "");
}
