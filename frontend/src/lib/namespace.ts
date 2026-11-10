import type { MyNamespaceEntry } from "../api";
import { parseChTime } from "./format";
import type { View } from "./state";

/**
 * Which namespace the dashboard lands on.
 *
 * The JWT `namespaces` claim is an access list, not a preference: its
 * first entry is whatever the token minter wrote first (a bot corpus
 * such as a public shared namespace, or the wildcard `*`). Landing
 * there showed people someone else's memories, and `*` became a
 * literal namespace name that every query then read as
 * `WHERE namespace = '*'` — an empty dashboard with no error.
 *
 * Order, first match wins, never a collection (`col-…`, which the Library holds):
 *   1. the saved pick, if the user can still reach it;
 *   2. a namespace the user administers — `namespace.list_mine` carries
 *      no owner field, so `role === "admin"` is the owner signal; the
 *      server's auto-claimed `personal-*` row first, then the oldest
 *      admin grant (the one the user created first);
 *   3. the first claim that names a real namespace;
 *   4. nothing — the caller lands on the Library when the account reads a shared
 *      collection, else on Setup; it never invents a name.
 *
 * Code indexes and tracker namespaces (`kind` code / qa) are reference data,
 * never a memory identity and never the landing *default* while a memory namespace is
 * reachable; a claim on one is still reachable — a saved pick of `code-clickhouse` is
 * honoured, because the Explorer's Patterns mode lives there. `*` is never a namespace
 * name.
 *
 * A token that names ONLY code and/or QA namespaces — a QA team, a reviewer, a code-index
 * service account — is a real persona, not a misconfiguration. On the bare root it lands on
 * Repositories (`landingTarget`) with the picker on its first reachable code or tracker
 * namespace (code before qa, alphabetical within); one that reaches only shared
 * collections lands on the Library, and one that reaches nothing else, or only its own
 * collection, lands on Setup. An explicit path always wins (`deepLinkNamespace`): a deep
 * link keeps its view, and when the default-namespace chain finds no namespace the view
 * takes the first reachable namespace of the kind it needs — a memory view a memory one
 * (none → its own empty state), every other view a code or QA one.
 */
export const WILDCARD_NAMESPACE = "*";

/** A namespace name as the server takes one (`server/core/naming.py`): 1–128 lowercase
 *  letters, digits and hyphens, starting with a letter or digit. */
export const NAMESPACE_NAME = /^[a-z0-9][a-z0-9-]{0,127}$/;

/** A collection's name starts with this (server/core/principals.py COLLECTION_PREFIX). */
export const COLLECTION_PREFIX = "col-";

/** Whether `name` is a collection's name (`col-…`, server/auth/sharing.py), as a link may
 *  name one: anything else a link carries is not shown. */
export function isCollectionName(name: string | null | undefined): name is string {
  return !!name && name.startsWith(COLLECTION_PREFIX) && NAMESPACE_NAME.test(name);
}

/** A name the dashboard may switch into: a real namespace, whatever its kind. */
function isReachableNamespace(m: Pick<MyNamespaceEntry, "namespace">): boolean {
  return m.namespace !== WILDCARD_NAMESPACE && m.namespace !== "";
}

/** A reference-data kind: a code index or a tracker namespace. */
export function isReferenceKind(kind: MyNamespaceEntry["kind"] | string | null | undefined): boolean {
  // the tracker registry still writes kind "qa" for a tracker namespace
  return kind === "code" || kind === "tracker" || kind === "qa";
}

/** A namespace that may be the memory landing default: reachable and a
 *  memory identity — a repository, a tracker namespace or a collection never is (a
 *  collection opens in the Library, by its reserved `col-` name). */
export function isSelectableNamespace(m: Pick<MyNamespaceEntry, "namespace" | "kind">): boolean {
  return isReachableNamespace(m) && !isReferenceKind(m.kind) && !isCollectionName(m.namespace);
}

/** The server's `kind` for `namespace`, from the list_mine rows the app
 *  holds; undefined when the rows do not name it (or were never loaded). */
export function kindOf(
  namespace: string,
  mine: readonly MyNamespaceEntry[] | null,
): MyNamespaceEntry["kind"] | undefined {
  return mine?.find((m) => m.namespace === namespace)?.kind;
}

/** True when `namespace` is a name a memory view may read from: non-empty
 *  and not a code or tracker namespace, nor a collection (the Library shows those). An
 *  unknown kind (no list_mine row, or list_mine failed) reads as memory. */
export function isMemoryNamespace(namespace: string, mine: readonly MyNamespaceEntry[] | null): boolean {
  return (
    namespace !== "" &&
    namespace !== WILDCARD_NAMESPACE &&
    !isCollectionName(namespace) &&
    !isReferenceKind(kindOf(namespace, mine))
  );
}

/** Views that read the memory plane of the picked namespace. Everything else —
 *  Repositories, Graph, Discover, Setup, the admin views — reads the org, the code plane
 *  or the tracker plane and works on a code or tracker namespace. */
const MEMORY_VIEWS: ReadonlySet<View> = new Set<View>([
  "memories",
  "recall",
  "profile",
  "knowledge",
  "sharing",
  "sessions",
  "runs",
  // An inbox is owned by a memory namespace.
  "tasks",
]);

export function needsMemoryNamespace(view: View): boolean {
  return MEMORY_VIEWS.has(view);
}

/** The set the picker may switch into: real claims plus list_mine rows. */
export function reachableNamespaces(claims: readonly string[], mine: readonly MyNamespaceEntry[]): Set<string> {
  const allowed = new Set(claims.filter((ns) => ns !== WILDCARD_NAMESPACE && ns !== ""));
  for (const m of mine) {
    if (isReachableNamespace(m)) allowed.add(m.namespace);
  }
  return allowed;
}

/** Whether a link may open `namespace`, from what the picker lists: a claim names it, or a
 *  list_mine row is an active grant on it. A pending request reads nothing, and without
 *  rows (list_mine failed) only the claims count. A token whose claim is `*` reads more than
 *  it lists; like the picker, a link to a namespace it does not list finds no access. */
export function canReadNamespace(
  namespace: string,
  claims: readonly string[],
  mine: readonly MyNamespaceEntry[] | null,
): boolean {
  return (
    claims.includes(namespace) || !!mine?.some((m) => m.namespace === namespace && (m.status ?? "active") === "active")
  );
}

/** An end user's personal collection (`col-me-…`, server/core/principals.py's
 *  PERSONAL_PREFIX): opened by its grants only, and it takes no guide. */
export const PERSONAL_COLLECTION_PREFIX = "col-me-";

export function isPersonalCollection(ns: string): boolean {
  return ns.startsWith(PERSONAL_COLLECTION_PREFIX);
}

/** The auto-claimed personal namespace (`personal-<subject>`), the one row a
 *  brand-new account has before it creates or joins anything. */
export function isPersonalNamespace(ns: string): boolean {
  return ns.startsWith("personal-");
}

function ownNamespace(mine: readonly MyNamespaceEntry[]): string | null {
  const admin = mine.filter(
    (m) => m.role === "admin" && (m.status ?? "active") === "active" && isSelectableNamespace(m),
  );
  if (admin.length === 0) return null;
  const personal = admin.find((m) => isPersonalNamespace(m.namespace));
  if (personal) return personal.namespace;
  const grantedAt = (m: MyNamespaceEntry) => parseChTime(m.joined_at)?.getTime() ?? Infinity;
  const byOldestGrant = [...admin].sort((a, b) => grantedAt(a) - grantedAt(b));
  return byOldestGrant[0].namespace;
}

export function landingNamespace(
  saved: string | null,
  claims: readonly string[],
  mine: readonly MyNamespaceEntry[],
): string | null {
  const allowed = reachableNamespaces(claims, mine);
  // A collection is never the header's namespace: a saved one is passed over.
  if (saved && allowed.has(saved) && !isCollectionName(saved)) return saved;
  // A benchmark namespace is a default only when nothing else is reachable: it holds a
  // harness's rows, not the person's.
  const bench = new Set(mine.filter((m) => m.kind === "bench").map((m) => m.namespace));
  const own = ownNamespace(mine.filter((m) => !bench.has(m.namespace)));
  if (own) return own;
  // The first real claim — never a repository, a tracker namespace or a collection by default.
  const reference = new Set(mine.filter((m) => !isSelectableNamespace(m)).map((m) => m.namespace));
  const usable = claims.filter((ns) => allowed.has(ns) && !reference.has(ns) && !isCollectionName(ns));
  return usable.find((ns) => !bench.has(ns)) ?? ownNamespace(mine) ?? usable[0] ?? null;
}

/** The reachable code and tracker namespaces, in the order the dashboard offers
 *  them as a landing: code indexes first, then tracker namespaces, alphabetical
 * within each. The kind comes from list_mine — grant rows and claim-only rows alike carry
 *  it — so a claim with no row is not here. */
export function referenceNamespaces(claims: readonly string[], mine: readonly MyNamespaceEntry[]): string[] {
  const allowed = reachableNamespaces(claims, mine);
  const rank = (kind: MyNamespaceEntry["kind"]) => (kind === "code" ? 0 : 1);
  return mine
    .filter((m) => allowed.has(m.namespace) && isReferenceKind(m.kind) && (m.status ?? "active") === "active")
    .sort((a, b) => rank(a.kind) - rank(b.kind) || a.namespace.localeCompare(b.namespace))
    .map((m) => m.namespace)
    .filter((ns, i, all) => all.indexOf(ns) === i);
}

/** Where the bare root lands. `view` is null when the default-namespace chain found a memory
 *  namespace — the caller keeps its own default view for that case. */
export interface LandingTarget {
  view: "code" | "knowledge" | "setup" | null;
  namespace: string | null;
}

export function landingTarget(
  saved: string | null,
  claims: readonly string[],
  mine: readonly MyNamespaceEntry[],
): LandingTarget {
  const ns = landingNamespace(saved, claims, mine);
  if (ns) return { view: isReferenceKind(kindOf(ns, mine)) ? "code" : null, namespace: ns };
  const reference = referenceNamespaces(claims, mine);
  if (reference.length > 0) return { view: "code", namespace: reference[0] };
  return { view: "setup", namespace: null };
}

/** The namespace a deep link opens on: the default-namespace chain when it finds one;
 *  otherwise the first reachable code or tracker namespace for a view that does not need
 *  the memory plane, and nothing for one that does — that view renders its own empty
 *  state. The view itself is never changed by the namespace rule. */
export function deepLinkNamespace(
  view: View,
  saved: string | null,
  claims: readonly string[],
  mine: readonly MyNamespaceEntry[],
): string | null {
  const ns = landingNamespace(saved, claims, mine);
  if (ns) return ns;
  if (needsMemoryNamespace(view)) return null;
  return referenceNamespaces(claims, mine)[0] ?? null;
}
