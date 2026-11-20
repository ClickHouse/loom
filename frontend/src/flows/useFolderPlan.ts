import { useMemo, useRef } from "react";
import type { CollectionEntry } from "../api";
import type { BatchRow } from "../lib/batchIngest";
import { withTwinNames } from "../lib/collectionPlaces";
import { collectionTitle, repeatedTitles } from "../lib/collections";
import { collectionsMade, folderLabel, lineageLength, planFolders, tooDeep } from "../lib/folderUpload";

/** The collections a batch's folders become, parents first, and their names on screen; from
 *  the pick and the list as the dialog opened, never from the rows as they change. */
export function useFolderPlan(
  tree: { rows: CollectionEntry[]; shown: CollectionEntry | null } | undefined,
  folderFiles: BatchRow[],
  nestable: boolean,
  asCollections: boolean,
) {
  const folderish = folderFiles.length > 0;
  // The dropped folders sit in `root`.
  const root = nestable && tree?.shown ? tree.shown.namespace : "";
  // `tree` is held from the first render, so its rows are too.
  const treeRows = useMemo(() => tree?.rows ?? [], [tree]);
  // The folders' collections, whichever way they are sent: the folders choice names them.
  const allPlans = useMemo(
    () =>
      tree && folderish
        ? planFolders(
            folderFiles.map((r) => r.key),
            treeRows,
            nestable ? (tree?.shown ?? null) : null,
          )
        : [],
    [tree, folderish, folderFiles, treeRows, nestable],
  );
  const plans = useMemo(() => (asCollections ? allPlans : []), [asCollections, allPlans]);
  const byFolder = useMemo(() => new Map(plans.map((p) => [p.path, p])), [plans]);
  // A folder's own plan, or that of the sibling with the same title whose collection it is.
  const planOf = (folder: string) => {
    const p = byFolder.get(folder);
    return p?.sameAs ? byFolder.get(p.sameAs) : p;
  };
  // Worked out once: the list redraws on every step of an upload.
  const repeated = useMemo(() => repeatedTitles(treeRows), [treeRows]);
  const listedBy = useMemo(() => new Map(treeRows.map((t) => [t.namespace, t])), [treeRows]);
  // On My files or a namespace, the top-level collections the dropped folders become.
  const topPlans = useMemo(
    () => (nestable ? [] : collectionsMade(allPlans).filter((p) => !p.parentPath)),
    [nestable, allPlans],
  );
  const labels = useMemo(
    () => new Map(plans.map((p) => [p.path, folderLabel(planOf(p.path) ?? p, treeRows, repeated)])),
    [plans, byFolder, treeRows, repeated],
  );
  const folderPlace = (folder: string) => labels.get(folder) ?? folder;
  // A collection the check read, by the name its folder shows it with.
  const cutNames = new Map(plans.filter((p) => p.existing).map((p) => [p.existing, folderPlace(p.path)]));
  // Where a folder went: the collection's title, or its address when a listed one looks the same.
  const wentNames = useRef(new Map<string, string>());
  const wentTo = (folder: string, went: { namespace: string; title: string }) => {
    if (went.namespace === planOf(folder)?.existing) return folderPlace(folder);
    const title = went.title || went.namespace;
    const key = `${went.namespace}\0${title}`;
    let name = wentNames.current.get(key);
    if (name === undefined) {
      const listed = (ns: string) => {
        const row = listedBy.get(ns);
        return ns === went.namespace || !row ? title : collectionTitle(row);
      };
      name = withTwinNames(listed, [...listedBy.keys(), went.namespace])(went.namespace);
      wentNames.current.set(key, name);
    }
    return name;
  };
  const deep = useMemo(
    () => (tree ? tooDeep(plans, nestable ? lineageLength(tree.shown, treeRows) : 0) : []),
    [plans, nestable, tree, treeRows],
  );
  return { root, treeRows, plans, byFolder, planOf, repeated, topPlans, folderPlace, cutNames, wentTo, deep };
}
