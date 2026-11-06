/**
 * Where a passage sits in its document, in words: the locator a knowledge hit carries
 * (`server/knowledge/domain/extract/artifact.py::Locator`), read for its format. A deck's
 * pages are slides; a sheet's, a CSV's or a Parquet file's rows are rows.
 */

/** A hit's locator as the server sends it: only the fields that apply are present. */
export interface Locator {
  line_start?: number;
  line_end?: number;
  char_start?: number;
  char_end?: number;
  key_path?: string;
  page_start?: number;
  page_end?: number;
  sheet?: string;
  row_start?: number;
  row_end?: number;
}

function span(one: string, many: string, start?: number, end?: number): string {
  if (!start) return "";
  return end && end !== start ? `${many} ${start}–${end}` : `${one} ${start}`;
}

/** "lines 10–24", "page 3", "slides 2–3", "Orders, rows 2–40", "key server.port", or
 *  "" for a hit with no locator (text a caller extracted). */
export function formatLocator(format: string, locator: Locator | null | undefined): string {
  if (!locator) return "";
  // A locator places a passage one way (an extractor sets a key, pages or a sheet's rows,
  // with lines beside a key); the first that applies is the most useful.
  if (locator.key_path) return `key ${locator.key_path}`;
  const pages =
    format === "pptx"
      ? span("slide", "slides", locator.page_start, locator.page_end)
      : span("page", "pages", locator.page_start, locator.page_end);
  if (pages) return pages;
  const rows = [locator.sheet ?? "", span("row", "rows", locator.row_start, locator.row_end)]
    .filter(Boolean)
    .join(", ");
  if (rows) return rows;
  return span("line", "lines", locator.line_start, locator.line_end);
}

/** The passage's citation: the revision it was read from and where it sits in it. */
export function citation(hit: { format?: string; revision?: number; locator?: Locator | null }): string {
  const where = formatLocator(hit.format ?? "", hit.locator);
  const rev = hit.revision ? `revision ${hit.revision}` : "";
  return [rev, where].filter(Boolean).join(", ");
}
