// Which memory namespaces the namespace picker folds into a group, by the prefix their
// names share. Only the picker's open panel (components/NamespacePickerList.tsx) reads it,
// so it loads with that panel.

// A name's tokens: the runs between its hyphens, a uuid (8-4-4-4-12 hex) one token.
const NAME_TOKEN = /[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![^-])|[^-]+/g;

/** Which of `names` fold into a group in the namespace picker, and under which prefix:
 *  each name goes to the longest token prefix (`team-`, `agent-<uuid>-`) that `min` or more
 *  of the names share, and a prefix left with fewer once its names took longer ones folds
 *  none. A uuid is one token, so names built as `<kind>-<owner uuid>-<id>` fold per owner. */
export function nameFamilies(names: readonly string[], min = 3): Map<string, string> {
  // A name's prefixes as written (doubled hyphens stay doubled): up to each token but the first.
  const prefixes = names.map((name) => [...name.matchAll(NAME_TOKEN)].slice(1).map((m) => name.slice(0, m.index)));
  const shared = tally(prefixes.flat());
  const family = new Map<string, string>();
  names.forEach((name, i) => {
    const longest = prefixes[i].filter((p) => (shared.get(p) ?? 0) >= min).pop();
    if (longest) family.set(name, longest);
  });
  const size = tally([...family.values()]);
  for (const [name, prefix] of family) if ((size.get(prefix) ?? 0) < min) family.delete(name);
  return family;
}

function tally(items: readonly string[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const item of items) out.set(item, (out.get(item) ?? 0) + 1);
  return out;
}
