// Single-key shortcuts (?, /, g then a letter, j, k and the Tasks keys) can be turned off
// in the help dialog (WCAG 2.1.4): speech input types letters that would otherwise fire
// them. A per-browser preference; Escape, the arrows and ⌘K are not character keys and
// stay on.
const KEY = "loom_single_key_shortcuts_v1";

export function singleKeyShortcutsOn(): boolean {
  try {
    return localStorage.getItem(KEY) !== "off";
  } catch {
    return true;
  }
}

export function setSingleKeyShortcuts(on: boolean): void {
  try {
    if (on) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, "off");
  } catch {
    /* storage refused: the shortcuts stay as they are */
  }
}

/** A key the switch covers: one printable character, no modifier. */
export function isSingleKeyShortcut(e: KeyboardEvent): boolean {
  return e.key.length === 1 && e.key !== " " && !e.metaKey && !e.ctrlKey && !e.altKey;
}

/** True when the key is a single-key shortcut and those are turned off. */
export function shortcutOff(e: KeyboardEvent): boolean {
  return isSingleKeyShortcut(e) && !singleKeyShortcutsOn();
}
