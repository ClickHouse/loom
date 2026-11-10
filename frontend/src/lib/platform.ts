/** Whether the keyboard is an Apple one (⌘ rather than Ctrl): macOS, iOS and iPadOS. */
export function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  const platform = nav.userAgentData?.platform || nav.platform || nav.userAgent || "";
  return /mac|iphone|ipad|ipod/i.test(platform);
}

/** The search palette's shortcut as the keyboard prints it: `⌘K` on Apple, `Ctrl K` elsewhere. */
export function paletteShortcut(): string {
  return isApplePlatform() ? "⌘K" : "Ctrl K";
}

/** The assistant panel's shortcut as the keyboard prints it: `⌘ /` on Apple, `Ctrl /` elsewhere. */
export function assistantShortcut(): string {
  return isApplePlatform() ? "⌘ /" : "Ctrl /";
}
