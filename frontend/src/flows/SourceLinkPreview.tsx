import type { KnowledgeSourceParsedLink } from "../api/knowledgeSources";
import { KIND_OPTIONS } from "../lib/knowledgeSources";

const ITEM_LABELS: Record<string, string> = {
  folder: "Folder",
  file: "File",
  shared_drive: "Shared drive",
  filter: "Files matching a filter",
  page: "Page",
  database: "Database",
  project: "Project",
};

function providerLabel(kind: string): string {
  return KIND_OPTIONS.find((option) => option.value === kind)?.label ?? "another provider";
}

function linkProblem(link: KnowledgeSourceParsedLink, expectedKind?: string): string | null {
  if (link.unreadable) return link.unreadable;
  if (!link.kind || !Object.keys(link.item).length) return "This link did not resolve to supported content.";
  if (expectedKind && link.kind !== expectedKind)
    return `This is ${providerLabel(link.kind)} content. Choose that provider or remove this link.`;
  return null;
}

export function validateSourceLinkPreview(
  links: KnowledgeSourceParsedLink[],
  expectedKind: string,
  requestedLinks: readonly string[],
): { matches: boolean; problems: string[] } {
  if (links.length !== requestedLinks.length || links.some((row, i) => row.link !== requestedLinks[i]))
    return { matches: false, problems: ["The link preview did not match your selection. Check the links again."] };
  return {
    matches: true,
    problems: links.flatMap((row) => {
      const problem = linkProblem(row, expectedKind);
      return problem ? [`${row.link}: ${problem}`] : [];
    }),
  };
}

/** Every selected link keeps its own result; resolving a type proves no provider access. */
export function SourceLinkPreview({
  links,
  expectedKind,
}: {
  links: KnowledgeSourceParsedLink[];
  expectedKind?: string;
}) {
  const problems = links.filter((row) => linkProblem(row, expectedKind));
  return (
    <section aria-label="Content link preview" className="space-y-2 text-sm">
      <p role="status">
        {links.length} {links.length === 1 ? "link" : "links"} previewed.
        {problems.length > 0 && ` ${problems.length} ${problems.length === 1 ? "needs" : "need"} attention.`}
      </p>
      <p className="text-xs text-text-muted">
        This preview identifies content. It does not check access or import documents.
      </p>
      <ul className="space-y-2" aria-label="Link results">
        {links.map((row, index) => {
          const problem = linkProblem(row, expectedKind);
          return (
            <li key={`${index}:${row.link}`} className="min-w-0 break-words">
              <p className="font-mono text-xs">{row.link}</p>
              {problem ? (
                <p className="text-xs text-text-muted">Cannot use this link: {problem}</p>
              ) : (
                <p className="text-xs text-text-muted">
                  {providerLabel(row.kind)} · {ITEM_LABELS[String(row.item.type)] ?? "Selected content"}
                  {typeof row.item.id === "string" && ` · ${row.item.id}`}
                  {row.item.recursive === true && " · with subfolders"}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
