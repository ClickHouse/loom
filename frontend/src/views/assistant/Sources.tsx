import { useId } from "react";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { Popover } from "@clickhouse/click-ui/Popover";
import type { Citation, Support } from "../../api/assistantChat";
import { IconButton } from "../../components/IconButton";
import { PopoverTrigger } from "../../components/ui/Menu";
import { useStore } from "../../lib/state";

/**
 * One finished answer's label and citations, as the server checked them against Loom with
 * the answer's token. The label follows the answer's `support` (server/assistant/domain/
 * support.py): an answer that makes no claim, such as a greeting or an abstention, has none.
 * A verified memory or document opens where the dashboard shows it; an unverified citation
 * says why.
 */

interface Label {
  text: string;
  why: string;
  icon: IconName;
  tone: string;
}

/** The label of an answer with this support and these citations; null when it has none. */
export function supportLabel(support: Support, citations: Citation[]): Label | null {
  const verified = citations.filter((c) => c.verified).length;
  if (support === "none") return null;
  if (support === "supported") {
    const all = verified === citations.length;
    return {
      text: all
        ? "Checked against what Loom holds"
        : `Checked against what Loom holds: ${verified} of ${citations.length} sources`,
      why: all
        ? "Loom checked each source this answer cites against what it holds and what the answer's own reads returned."
        : "Loom checked each source this answer cites; the ones marked Not verified did not check out, and each says why.",
      icon: "check-in-circle",
      tone: "text-text-muted",
    };
  }
  if (support === "unsupported") {
    return citations.length > 0
      ? {
          text: "Couldn't confirm: none of its sources checked out",
          why: "None of the sources this answer cites matched what Loom holds or what the answer read. Each source below says why.",
          icon: "warning",
          tone: "text-warn",
        }
      : {
          text: "Couldn't confirm: it gives no sources",
          why: "This answer read rows from Loom but cites none of them, so nothing it says could be checked.",
          icon: "warning",
          tone: "text-warn",
        };
  }
  // "unchecked", and any value this dashboard does not know yet.
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

export function Sources({ citations, support, answer }: { citations: Citation[]; support: Support; answer: number }) {
  const inspect = useStore((s) => s.inspect);
  const openDocument = useStore((s) => s.openDocument);
  const labelId = useId();
  const label = supportLabel(support, citations);
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
          {citations.map((c) => (
            <li key={c.text} className="break-words">
              <span className="text-text-muted">{c.verified ? "Verified" : "Not verified"} · </span>
              {c.verified && c.ref && (c.kind === "memory" || c.kind === "doc") ? (
                <Link
                  component="button"
                  type="button"
                  size="sm"
                  // A cited key may be any writer's: the wildcard reads it whichever agent,
                  // user or session it is filed under.
                  onClick={() => (c.kind === "memory" ? inspect(c.ref, { agent: "*" }) : openDocument(c.ref))}
                >
                  {citationText(c)}
                </Link>
              ) : (
                <span className={c.verified ? "text-text" : "text-text-muted"}>{citationText(c)}</span>
              )}
              {!c.verified && c.reason && <span className="text-text-muted"> ({c.reason})</span>}
            </li>
          ))}
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
