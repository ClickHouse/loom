/**
 * Colour theme — Click UI's dark and light token sets (styles.css).
 *
 * Dark is the default and needs no attribute; light is `<html data-theme="light">`.
 * The choice is a per-browser convenience, so it lives in localStorage and a blocked or
 * empty storage simply means dark.
 */
export type Theme = "dark" | "light";

const KEY = "loom_theme";

export function storedTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export function setTheme(theme: Theme): void {
  applyTheme(theme);
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // Storage blocked (private window, site data off): the theme holds for this tab only.
  }
}
