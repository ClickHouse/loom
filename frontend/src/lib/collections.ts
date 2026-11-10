/**
 * What a collection is called on screen, and the name a title derives.
 *
 * A collection's name (`col-…`) is its address: grants, search scope and audit key on it,
 * and it is never used again once deleted. People see its title, which may repeat; the
 * server derives the name from the title (`collection.create` with `derive_name`,
 * server/identity/domain/collection_names.py).
 */
import type { CollectionEntry } from "../api";
import { COLLECTION_PREFIX as PREFIX, PERSONAL_COLLECTION_PREFIX as PERSONAL_PREFIX } from "./namespace";

const SLUG_LEN = 40;

const ROLE_LABEL: Record<string, string> = { viewer: "Viewer", writer: "Writer", admin: "Admin" };

/** How many collections the Library reads (`collection.list`), in pages of a size. */
export const COLLECTIONS_PAGE = 500;
export const COLLECTIONS_PAGES = 20;
export const COLLECTIONS_LISTED = COLLECTIONS_PAGE * COLLECTIONS_PAGES;

/** The person's role in a collection, in words. */
export function roleLabel(role: string): string {
  return ROLE_LABEL[role] ?? role;
}

/** The order collections are listed in: the user's own first, then by what is shown,
 *  then by name. */
export function collectionOrder(a: CollectionEntry, b: CollectionEntry): number {
  return (
    Number(b.personal) - Number(a.personal) ||
    collectionTitle(a).localeCompare(collectionTitle(b)) ||
    a.namespace.localeCompare(b.namespace)
  );
}

/** A collection's title, "My files" for the user's own, else its name. */
export function collectionTitle(c: Pick<CollectionEntry, "namespace" | "personal" | "title">): string {
  if (c.personal) return "My files";
  return c.title?.trim() || c.namespace;
}

/** What makes two titles the same title, as on the server (collection_names.py
 *  `title_key`): case, runs of whitespace, Unicode spelling and the characters that may
 *  draw as nothing do not count, but the joiners and tags that shape emoji and scripts do. */
export function titleKey(title: string): string {
  return fold(
    title.normalize("NFKC").replace(/(?![\u200c\u200d\u{e0020}-\u{e007f}])\p{Default_Ignorable_Code_Point}/gu, ""),
  );
}

/** What two titles share when they look alike on screen: as `titleKey`, with every
 *  character that may draw as nothing dropped, the joiners too. Titles that look alike
 *  are shown with their addresses. */
function looksKey(title: string): string {
  return fold(title.normalize("NFKC").replace(/\p{Default_Ignorable_Code_Point}/gu, ""));
}

function fold(text: string): string {
  // Upper then lower case folds "ß" as the server's casefold does.
  return text.trim().split(/\s+/).join(" ").toUpperCase().toLowerCase();
}

/** The titles shown more than once among `rows`: those are printed with their name. */
export function repeatedTitles(rows: CollectionEntry[]): Set<string> {
  const seen = new Set<string>();
  const twice = new Set<string>();
  for (const c of rows) {
    const k = looksKey(collectionTitle(c));
    if (seen.has(k)) twice.add(k);
    seen.add(k);
  }
  return twice;
}

/** The title, with the name beside it when another collection shows the same title. */
export function distinctTitle(c: CollectionEntry, repeated: Set<string>): string {
  const t = collectionTitle(c);
  return repeated.has(looksKey(t)) && t !== c.namespace ? `${t} (${c.namespace})` : t;
}

/** A name for a title with no Latin letters or digits, sent only for a server a release
 *  behind to create (a current one derives its own): `col-c-` and six characters of a
 *  hash of the title, so two such titles do not share one name. Plain JS (FNV-1a), as
 *  WebCrypto is missing from a page served over plain HTTP. */
export function placeholderName(title: string): string {
  let h = 0x811c9dc5;
  for (const ch of titleKey(title)) {
    h ^= ch.codePointAt(0) ?? 0;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `${PREFIX}c-${h.toString(36).padStart(6, "0").slice(-6)}`;
}

/** The plain name a title derives, as the server derives it first: "Café notes" →
 *  `col-cafe-notes`. "" for a title with no Latin letters or digits, which the server names
 *  by a hash. */
export function plainName(title: string): string {
  // Case folded first (upper then lower, as the server's casefold), so "ß" reads as "ss".
  const slug = title
    .toUpperCase()
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^\x00-\x7f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, SLUG_LEN)
    .replace(/-+$/, "");
  if (!slug) return "";
  return `${PREFIX}${slug}-`.startsWith(PERSONAL_PREFIX) ? `${PREFIX}c-${slug}` : `${PREFIX}${slug}`;
}
