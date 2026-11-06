/** Naming an added document: a URI-safe slug of its title. */

/** A URI-safe slug of a title: lower-case words joined by `-`, at most 60 characters. */
export function slugify(title: string): string {
  return (
    title
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "document"
  );
}
