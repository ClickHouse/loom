import { dump, load } from "js-yaml";
import { fromMarkdown } from "mdast-util-from-markdown";

const descriptionLimit = 160;

function plainText(node) {
  if (node.type === "text" || node.type === "inlineCode") return node.value;
  if (node.type === "break") return " ";
  const label = (node.children ?? []).map(plainText).join("");
  return node.type === "link" && label === node.url ? "" : label;
}

function prefixAtBoundary(value, locale, granularity, limit) {
  let end = 0;
  for (const { index, segment } of new Intl.Segmenter(locale, { granularity }).segment(value)) {
    if (index + segment.length > limit) break;
    end = index + segment.length;
  }
  return value.slice(0, end).trimEnd();
}

function descriptionFrom(body, locale) {
  const minimumLength = locale === "ja" || locale === "zh-CN" ? 16 : 30;
  for (const node of fromMarkdown(body).children) {
    if (node.type !== "paragraph") continue;
    let prose = plainText(node).replace(/\s+/g, " ").trim();
    if (prose.endsWith(":") || prose.endsWith("：")) {
      const tail = [...new Intl.Segmenter(locale, { granularity: "sentence" }).segment(prose)].at(-1);
      prose = prose.slice(0, tail.index).trimEnd();
    }
    if (prose.length < minimumLength || [":::", "!!!", "???", "[!", "|"].some((prefix) => prose.startsWith(prefix)))
      continue;
    if (prose.length <= descriptionLimit) return prose;
    const sentences = prefixAtBoundary(prose, locale, "sentence", descriptionLimit);
    if (sentences.length >= minimumLength) return sentences;
    const words = prefixAtBoundary(prose, locale, "word", descriptionLimit - 1);
    return words ? `${words}…` : "";
  }
  return "";
}

export function withContentMetadata(text, { date, locale = "en" } = {}) {
  const match = /^---\r?\n([\s\S]*?)^---[\t ]*(?:\r?\n|$)/m.exec(text);
  const frontmatter = match?.index === 0 ? match : null;
  const metadata = frontmatter ? (load(frontmatter[1]) ?? {}) : {};
  if (typeof metadata !== "object" || Array.isArray(metadata)) return text;

  const body = frontmatter ? text.slice(frontmatter[0].length) : text;
  const updates = {};
  if (date && !Object.hasOwn(metadata, "lastModified")) updates.lastModified = date;
  const hasDescription = [metadata.description, metadata.seo?.description].some(
    (value) => typeof value === "string" && value.trim(),
  );
  if (!hasDescription) {
    const description = descriptionFrom(body, locale);
    if (description) updates.description = description;
  }
  if (!Object.keys(updates).length) return text;
  return `---\n${dump({ ...metadata, ...updates }, { lineWidth: -1 })}---\n${frontmatter ? body : `\n${body}`}`;
}
