import { useId } from "react";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { Popover } from "@clickhouse/click-ui/Popover";
import type { Citation, Support } from "../../api/assistantChat";
import { IconButton } from "../../components/IconButton";
import { PopoverTrigger } from "../../components/ui/Menu";
import { useStore } from "../../lib/state";

interface Label {
  text: string;
  why: string;
  icon: IconName;
  tone: string;
}

/** The label of an answer with this support and these citations; null when it has none.
 *  `unknownNames` are the op and setting names it wrote that the server neither serves nor
 *  documents, which make it unsupported whatever it cites. */
export function supportLabel(support: Support, citations: Citation[], unknownNames: string[] = []): Label | null {
  const verified = citations.filter((c) => c.verified).length;
  if (support === "none") return null;
  if (support === "unsupported" && unknownNames.length > 0) {
    const names = unknownNames.join(", ");
    return {
      text: `Couldn't confirm: it names ${names}, which this Loom neither serves nor documents`,
      why: "The answer names an operation or setting this Loom has no record of, so it may have made the name up. Check the name before you use it.",
      icon: "warning",
      tone: "text-warn",
    };
  }
  if (support === "supported") {
    const all = verified === citations.length;
    return {
      text: all
        ? "Checked against what Loom holds"
        : `Checked against what Loom holds: ${verified} of ${citations.length} sources verified`,
      why: all
        ? "Loom checked each source this answer cites against what it holds and what the answer's own reads returned."
        : `Loom verified ${verified} of the ${citations.length} sources this answer cites; each one not verified says why.`,
      icon: "check-in-circle",
      tone: "text-text-muted",
    };
  }
  if (support === "unsupported") {
    return citations.length > 0
      ? {
          text: "Couldn't confirm: none of its sources were verified",
          why: "None of the sources this answer cites was verified against what Loom holds or what the answer read. Each source below says why.",
          icon: "warning",
          tone: "text-warn",
        }
      : {
          text: "Couldn't confirm: it gives no sources",
          why: "This answer looked in Loom but gives no source for what it says, so nothing it says could be checked.",
          icon: "warning",
          tone: "text-warn",
        };
  }
  // "unchecked".
  return citations.length > 0
    ? {
        text: "Sources not checked",
        why: "Loom could not check this answer's sources. Each source below says why.",
        icon: "info-in-circle",
        tone: "text-text-muted",
      }
    : {
        text: "Not checked: it gives no sources Loom can check",
        why: "This answer cites nothing Loom can check: what it read has no citation form, or its reads could not be checked.",
        icon: "info-in-circle",
        tone: "text-text-muted",
      };
}

/**
 * One finished answer's label and citations, as the server checked them against Loom with
 * the answer's token. The label follows the answer's `support` (server/assistant/domain/
 * support.py): an answer that makes no claim, such as a greeting or an abstention, has none.
 * A verified memory or document opens where the dashboard shows it; an unverified citation
 * says why.
 */
export function Sources({
  citations,
  support,
  unknownNames = [],
  answer,
}: {
  citations: Citation[];
  support: Support;
  unknownNames?: string[];
  answer: number;
}) {
  const inspect = useStore((s) => s.inspect);
  const openDocument = useStore((s) => s.openDocument);
  const openDocsPage = useStore((s) => s.openDocsPage);
  const labelId = useId();
  const label = supportLabel(support, citations, unknownNames);
  // Where a verified citation opens: a memory in the drawer (any writer's: the wildcard
  // reads it whichever agent, user or session it is filed under), a document in its
  // preview, a docs page on the Help view at its section.
  const opener = (c: Citation): (() => void) | undefined => {
    if (!c.verified || !c.ref) return undefined;
    if (c.kind === "memory") return () => inspect(c.ref, { agent: "*" });
    if (c.kind === "doc") return () => openDocument(c.ref);
    if (c.kind === "help") return () => openDocsPage(c.ref, c.anchor);
    return undefined;
  };
  if (!label && citations.length === 0) return null;
  return (
    <div className="mt-2 space-y-1" data-testid="assistant-sources">
      {label && (
        <div className={`flex items-center gap-1 text-xs ${label.tone}`} data-testid="assistant-support">
          <Icon name={label.icon} size="sm" aria-hidden="true" />
          <span id={labelId}>{label.text}</span>
          <Popover>
            <PopoverTrigger>
              <IconButton
                icon="info-in-circle"
                size="sm"
                label={`Why this label, answer ${answer}`}
                aria-describedby={labelId}
              />
            </PopoverTrigger>
            <Popover.Content align="start" aria-label="Why this label" className="w-[min(320px,90vw)]">
              <p className="text-xs text-text-muted">{label.why}</p>
            </Popover.Content>
          </Popover>
        </div>
      )}
      {citations.length > 0 && (
        <ul className="space-y-1 text-xs" aria-label="Sources">
          {citations.map((c) => {
            const open = opener(c);
            return (
              <li key={c.text} className="break-words">
                <span className="text-text-muted">{c.verified ? "Verified" : "Not verified"} · </span>
                {open ? (
                  <Link component="button" type="button" size="sm" onClick={open}>
                    {citationText(c)}
                  </Link>
                ) : (
                  <span className={c.verified ? "text-text" : "text-text-muted"}>{citationText(c)}</span>
                )}
                {!c.verified && c.reason && <span className="text-text-muted"> ({c.reason})</span>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function citationText(c: Citation): string {
  if (c.kind === "sql") {
    // The statement's first line tells one query of the answer from another.
    const first =
      c.text
        .replace(/^```sql\s*/i, "")
        .trim()
        .split("\n")[0] ?? "";
    return `SQL query: ${first.length > 80 ? `${first.slice(0, 79)}…` : first}`;
  }
  return c.text.replace(/^\[|\]$/g, "");
}
