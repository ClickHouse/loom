import { useEffect, useState } from "react";
import type { MyNamespaceEntry } from "../api";
import { sharedMine } from "./sharedReads";
import { useStore } from "./state";

/** The caller's grant rows in `org`, for a gate that wants the direct grant even under an
 *  admin claim, whose role read skips list_mine: the store's rows, else, when `enabled`, one
 *  shared list_mine of this page's own. `loading` until either answers; null rows after a
 *  failed read. */
export function useGrantRows(org: string, enabled: boolean): { rows: MyNamespaceEntry[] | null; loading: boolean } {
  const myNamespaces = useStore((s) => s.myNamespaces);
  const [read, setRead] = useState<{ org: string; rows: MyNamespaceEntry[] | null } | null>(null);
  useEffect(() => {
    if (!enabled || myNamespaces !== null) return;
    let live = true;
    sharedMine(org).then(
      (r) => live && setRead({ org, rows: r }),
      () => live && setRead({ org, rows: null }),
    );
    return () => {
      live = false;
    };
  }, [enabled, org, myNamespaces]);
  if (myNamespaces !== null) return { rows: myNamespaces, loading: false };
  if (read?.org !== org) return { rows: null, loading: enabled };
  return { rows: read.rows, loading: false };
}
