import { useState } from "react";

// Panels that start folded to their title row remember, per browser, that someone opened them.
const key = (name: string) => `loom_fold_${name}_v1`;

export function foldOpen(name: string): boolean {
  try {
    return localStorage.getItem(key(name)) === "open";
  } catch {
    return false;
  }
}

export function setFoldOpen(name: string, open: boolean): void {
  try {
    if (open) localStorage.setItem(key(name), "open");
    else localStorage.removeItem(key(name));
  } catch {
    /* storage refused: the panel folds again next time */
  }
}

/** A panel's open state, folded until someone opens it. */
export function useFold(name: string): [boolean, () => void] {
  const [open, setOpen] = useState(() => foldOpen(name));
  const toggle = () => {
    setFoldOpen(name, !open);
    setOpen(!open);
  };
  return [open, toggle];
}
