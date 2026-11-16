// The Library's collection picker as its specs render it: collections, the list's state, the
// picker as Documents mounts it, a window that matches one query, and a wait for its reads.
import { useState, type ComponentProps } from "react";
import { vi } from "vitest";
import type { CollectionEntry } from "../api";
import { repeatedTitles } from "../lib/collections";
import { LibraryCollections, type ReadableCollections } from "../views/LibraryCollections";
import { act } from "./rtl";

export const entry = (namespace: string, over: Partial<CollectionEntry> = {}): CollectionEntry => ({
  namespace,
  parent: "",
  personal: false,
  role: "admin",
  description: "",
  ...over,
});

export const ROWS = [
  entry("col-plans", { parent: "col-q3", role: "viewer" }),
  entry("col-q3", { description: "Quarter plans" }),
  entry("col-me-1a2b", { personal: true }),
];

export const read = (rows: CollectionEntry[] | null, over: Partial<ReadableCollections> = {}): ReadableCollections => ({
  rows,
  truncated: false,
  kept: rows && { rows, truncated: false },
  refreshing: false,
  error: null,
  reload: vi.fn(),
  retry: vi.fn(),
  expected: "",
  ...over,
});

/** A window where only `query` matches. */
export function matchOnly(query: string) {
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: q === query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

export async function settle() {
  for (let i = 0; i < 3; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
}

/** The picker as Documents mounts it: the New collection dialog's open state is the page's. */
export function Picker(
  props: Omit<
    ComponentProps<typeof LibraryCollections>,
    "creating" | "onCreatingChange" | "onCreated" | "claimCreateVersion" | "repeated" | "byName"
  >,
) {
  const [creating, setCreating] = useState(false);
  const rows = props.collections.rows ?? [];
  return (
    <LibraryCollections
      {...props}
      repeated={repeatedTitles(rows)}
      byName={new Map(rows.map((c) => [c.namespace, c]))}
      creating={creating}
      onCreatingChange={setCreating}
      claimCreateVersion={() => 0}
      onCreated={vi.fn()}
    />
  );
}
