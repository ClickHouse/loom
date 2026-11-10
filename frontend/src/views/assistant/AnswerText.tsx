import { Table } from "@clickhouse/click-ui/Table";
import { Fragment, type ReactNode } from "react";
import { CodeBlock } from "./CodeBlock";

/**
 * An assistant answer as text with Markdown emphasis, lists, tables and code, and nothing
 * else. The answer is model output that stored text can steer, so every piece of it is a
 * React text node: HTML in it shows as written, an image or a link shows as its Markdown
 * source, and a URL is not made a link. A citation (`[memory:<key>@v<version>]`,
 * `[doc:<uri> § <section>]`, `[help:<page> § <section>]`, `[code:<path>:<line>]`) shows
 * as written; the answer's Sources list links each one the server verified. A heading is
 * drawn as a bold line, keeping the page's heading outline its own.
 */
export function AnswerText({ text }: { text: string }) {
  return <div className="space-y-2 text-sm text-text break-words">{blocks(text)}</div>;
}

type Block =
  | { kind: "paragraph"; text: string; strong?: boolean }
  | { kind: "code"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "table"; header: string[]; rows: string[][] };

const FENCE = /^\s{0,3}(`{3,}|~{3,})/;
const HEADING = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;
const ITEM = /^\s*([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

function cells(line: string): string[] {
  const inner = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return inner.split("|").map((c) => c.trim());
}

/** The answer's blocks. A fence still open (the answer is streaming) runs to the end. */
export function parseBlocks(text: string): Block[] {
  const lines = text.split("\n");
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = FENCE.exec(line);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i++; // the closing fence
      out.push({ kind: "code", text: body.join("\n") });
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      out.push({ kind: "paragraph", text: heading[1], strong: true });
      i++;
      continue;
    }
    if (startsTable(lines, i)) {
      const header = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      out.push({ kind: "table", header, rows });
      continue;
    }
    const item = ITEM.exec(line);
    if (item) {
      const ordered = /\d/.test(item[1]);
      const items: string[] = [];
      while (i < lines.length) {
        const next = ITEM.exec(lines[i]);
        if (next && /\d/.test(next[1]) === ordered) {
          items.push(next[2]);
        } else if (lines[i].trim() && /^\s+/.test(lines[i]) && items.length) {
          items[items.length - 1] += `\n${lines[i].trim()}`; // a continuation line
        } else break;
        i++;
      }
      out.push({ kind: "list", ordered, items });
      continue;
    }
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !FENCE.test(lines[i]) &&
      !HEADING.test(lines[i]) &&
      !(para.length && (ITEM.test(lines[i]) || startsTable(lines, i)))
    )
      para.push(lines[i++]);
    out.push({ kind: "paragraph", text: para.join("\n") });
  }
  return out;
}

/** A table's header line at `i`: a line with a cell bar, then a rule line. */
function startsTable(lines: string[], i: number): boolean {
  return lines[i].includes("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]) && lines[i + 1].includes("-");
}

function blocks(text: string): ReactNode[] {
  return renderBlocks(text, inline);
}

/** `text`'s blocks, each line's inline Markdown drawn by `spans`: `inline` for an answer;
 *  the Help page passes one that also draws links into the docs. */
export function renderBlocks(text: string, spans: (line: string) => ReactNode[]): ReactNode[] {
  return parseBlocks(text).map((b, k) => {
    switch (b.kind) {
      case "code":
        return <CodeBlock key={k}>{b.text}</CodeBlock>;
      case "list": {
        const List = b.ordered ? "ol" : "ul";
        return (
          <List key={k} className={`${b.ordered ? "list-decimal" : "list-disc"} pl-5 space-y-1`}>
            {b.items.map((item, j) => (
              <li key={j} className="whitespace-pre-wrap">
                {spans(item)}
              </li>
            ))}
          </List>
        );
      }
      case "table": {
        const width = b.header.length;
        return (
          <div key={k} className="overflow-x-auto">
            <Table
              size="sm"
              headers={b.header.map((h) => ({ label: spans(h) }))}
              rows={b.rows.map((row, j) => ({
                id: String(j),
                items: Array.from({ length: width }, (_, c) => ({
                  label: <span className="whitespace-normal">{spans(row[c] ?? "")}</span>,
                })),
              }))}
            />
          </div>
        );
      }
      default:
        return (
          <p key={k} className={`whitespace-pre-wrap${b.strong ? " font-semibold" : ""}`}>
            {spans(b.text)}
          </p>
        );
    }
  });
}

// A citation first, so the emphasis rules never reach into a key or a path
// (`[code:server/__init__.py:3]`); then code spans, strong, then emphasis. An underscore
// emphasises only between word boundaries, so snake_case stays as written.
const INLINE =
  /(\[(?:memory|doc|help|code):[^\]\n]+\])|(`+)([^`]|[^`][\s\S]*?[^`])\2(?!`)|\*\*(?=\S)([\s\S]*?\S)\*\*|(?<!\w)__(?=\S)([\s\S]*?\S)__(?!\w)|\*(?=[^\s*])([\s\S]*?[^\s*])\*|(?<!\w)_(?=[^\s_])([\s\S]*?[^\s_])_(?!\w)/g;

// With a link renderer, also `[label](target)` and `![alt](target)`, last: where a code span
// or emphasis starts first it wins, and a link inside emphasis is read in its content. A
// label is never empty and may hold one level of brackets, single-backtick code spans and a
// backtick that closes nothing, so such a code span that opens in a label and closes past it
// stays code. Links do not nest: a link inside a link's label is the link, and the outer
// brackets and target are text; an image's alt shows a link in it as its label.
const INLINE_LINKS = new RegExp(
  `${INLINE.source}|(!?)\\[((?:[^\\[\\]\\n\`]|\`[^\`\\n]*\`|\`(?![^\`\\n]*\`)|\\[[^\\[\\]\\n]*\\])+)\\]\\(([^)\\s]+)(?:\\s+"[^"]*")?\\)`,
  "g",
);

/** How a page draws one link: its label already drawn, whether it is an image, its target. */
export type LinkRenderer = (label: ReactNode[], image: boolean, target: string) => ReactNode;

/** A line's inline Markdown as React nodes; anything else, HTML included, is text. An
 *  answer gets no links: only a caller that passes `link` (the Help page) draws them. */
export function inline(text: string, link?: LinkRenderer): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let k = 0;
  for (const m of text.matchAll(link ? INLINE_LINKS : INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const [whole, citation, , code, strong, strong2, em, em2, image, label, target] = m;
    if (link && label !== undefined) {
      const isImage = image === "!";
      let nested = false;
      const inner: LinkRenderer = (l, img, t) => {
        if (img) return link(l, img, t);
        if (isImage) return l;
        nested = true;
        return link(l, img, t);
      };
      const drawn = inline(label, inner);
      out.push(
        <Fragment key={k++}>
          {nested ? ["[", ...drawn, whole.slice(label.length + 1)] : link(drawn, isImage, target)}
        </Fragment>,
      );
    } else if (citation) {
      out.push(
        <span key={k++} className="font-mono" data-citation="">
          {citation}
        </span>,
      );
    } else if (code !== undefined) {
      out.push(
        <code key={k++} className="font-mono text-xs bg-surface-2 rounded px-1">
          {code}
        </code>,
      );
    } else if (strong ?? strong2) {
      out.push(<strong key={k++}>{inline(strong ?? strong2, link)}</strong>);
    } else if (em ?? em2) {
      out.push(<em key={k++}>{inline(em ?? em2, link)}</em>);
    } else {
      out.push(whole);
    }
    last = at + whole.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
