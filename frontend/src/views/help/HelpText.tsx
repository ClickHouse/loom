import type { MouseEvent, ReactNode } from "react";
import { Link } from "@clickhouse/click-ui/Link";
import type { HelpOutlineEntry } from "../../api/help";
import { inline, renderBlocks } from "../assistant/AnswerText";

/**
 * A page of Loom's documentation, drawn with the answer renderer's blocks plus what a page
 * needs and an answer must never get: headings with their anchors, and links between the
 * pages. Only a relative link into the docs (another page, or a section of this one)
 * becomes a link, on this view; every other target, an external URL or an image included,
 * shows as its text.
 * The page is this build's own text, read with the signed-in session.
 */
export function HelpText({
  path,
  text,
  outline,
  onFollow,
}: {
  path: string;
  text: string;
  /** The page's outline from help.get: each heading's anchor and how the page writes it. */
  outline: HelpOutlineEntry[];
  /** Called when a link into the docs is clicked, to move there without a reload. */
  onFollow?: (e: MouseEvent, href: string) => void;
}) {
  const spans = (line: string) => withLinks(line, path, onFollow);
  return <div className="space-y-2 text-sm text-text break-words">{sections(text, outline, spans)}</div>;
}

const FENCE = /^\s{0,3}(`{3,}|~{3,})/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;

/** The text cut at its headings. Each heading takes the anchor of the next entry of the
 *  page's outline written the same way: the server gives every anchor, so the page never
 *  works one out, and a heading the two read differently (a setext one, one inside HTML)
 *  takes none rather than shifting the rest. A `##` is an h2 under the page header's h1. */
function sections(text: string, outline: HelpOutlineEntry[], spans: (line: string) => ReactNode[]): ReactNode[] {
  let next = 0;
  const anchor = (heading: string): string | undefined => {
    const at = outline.findIndex((e, i) => i >= next && e.heading === heading);
    if (at < 0) return undefined;
    next = at + 1;
    return outline[at].anchor || undefined;
  };
  const out: ReactNode[] = [];
  let body: string[] = [];
  let fence: string | null = null;
  // The server's title is its outline's first heading; with no outline, the first heading.
  const title = outline.find((e) => e.heading)?.heading;
  let titled = false;
  let k = 0;
  const flush = () => {
    if (body.length) out.push(<div key={k++}>{renderBlocks(body.join("\n"), spans)}</div>);
    body = [];
  };
  for (const line of text.split("\n")) {
    const f = FENCE.exec(line);
    if (f) fence = fence === null ? f[1] : line.trim().startsWith(fence) ? null : fence;
    const h = fence === null && !f ? HEADING.exec(line) : null;
    if (!h) {
      body.push(line);
      continue;
    }
    flush();
    const isTitle = !titled && (title === undefined || h[2] === title);
    titled = true;
    if (isTitle) {
      // The server's title, which the page header shows: only its anchor stays.
      out.push(<span key={k++} id={anchor(h[2])} />);
      continue;
    }
    // Every later heading is a section under the page header's h1, a `#` one as an h2.
    const Tag = `h${Math.max(h[1].length, 2)}` as "h2" | "h3" | "h4" | "h5" | "h6";
    out.push(
      <Tag key={k++} id={anchor(h[2])} className="font-semibold text-text pt-2 scroll-mt-4">
        {spans(h[2])}
      </Tag>,
    );
  }
  flush();
  return out;
}

/** A line's inline Markdown, read by the answer renderer with links: a link into the docs
 *  is drawn as one, any other link or image as its label. */
export function withLinks(line: string, page: string, onFollow?: (e: MouseEvent, href: string) => void): ReactNode[] {
  return inline(line, (label, image, target) => {
    const href = image ? null : helpHref(target, page);
    return href ? (
      <Link href={href} size="sm" onClick={onFollow && ((e: MouseEvent) => onFollow(e, href))}>
        {label}
      </Link>
    ) : (
      <span>{label}</span>
    );
  });
}

/** The Help page's address for a relative link from `page` into the docs, or for an
 *  anchor on `page`; null for an external or absolute target, a non-page file, or one
 *  outside the indexed docs. */
export function helpHref(target: string, page: string): string | null {
  if (/^[a-z][a-z0-9+.-]*:|^\/|^#/i.test(target)) {
    return target.startsWith("#") ? `${pageHref(page)}${target}` : null;
  }
  const [file, anchor] = target.split("#", 2);
  if (!file.endsWith(".md")) return null;
  const parts = page.split("/").slice(0, -1);
  for (const part of file.split("/")) {
    if (part === "..") {
      if (!parts.length) return null; // above the docs
      parts.pop();
    } else if (part && part !== ".") parts.push(part);
  }
  const resolved = parts.join("/");
  if (resolved.startsWith("development/")) return null; // not indexed
  return `${pageHref(resolved)}${anchor ? `#${anchor}` : ""}`;
}

export function pageHref(page: string): string {
  return `/help?page=${encodeURIComponent(page)}`;
}
