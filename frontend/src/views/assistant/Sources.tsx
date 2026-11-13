import { useId } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { Popover } from "@clickhouse/click-ui/Popover";
import type { Citation, Support } from "../../api/assistantChat";
import { IconButton } from "../../components/IconButton";
import { PopoverTrigger } from "../../components/ui/Menu";
import { TipTarget } from "../../components/ui/TipTarget";
import { Tip } from "../../components/ui/Tooltip";
import { Unavailable } from "../../components/ui/Unavailable";
import { useStore } from "../../lib/state";
import { CITATION, citationKey } from "./AnswerText";

interface Label {
  text: string;
  why: string;
  icon: IconName;
  tone: string;
}

// Where a check looks for each cited source.
const IN_LOOM = "in what Loom holds, in this build's documentation or in what the answer's own reads returned";

/** The label of a finished answer whose check never came back: the connection was lost
 *  after its text, or the page was left while Loom checked it. */
export const NOT_CHECKED: Label = {
  text: "Check didn't finish",
  why: "Loom's check of this answer's sources didn't finish: the connection was lost or the page was left first, so nothing about its sources is known.",
  icon: "info-in-circle",
  tone: "text-text-muted",
};

/**
 * The label of an answer with this support and these citations; null when it has none.
 * When some sources were not found it leads with how many were. `unknownNames` are the op
 * and setting names it wrote that the server neither serves nor documents, which make it
 * unsupported whatever it cites.
 */
export function supportLabel(support: Support, citations: Citation[], unknownNames: string[] = []): Label | null {
  const found = citations.filter((c) => c.verified).length;
  const count = `${found} of ${citations.length} ${citations.length === 1 ? "source" : "sources"} found in Loom`;
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
    const all = found === citations.length;
    return {
      text: all ? "Checked against what Loom holds" : `Partly confirmed: ${count}`,
      why: all
        ? `Loom found each source this answer cites ${IN_LOOM}.`
        : `Loom found ${found} of the ${citations.length} sources this answer cites ${IN_LOOM}; each other source says why.`,
      icon: "check-in-circle",
      tone: "text-text-muted",
    };
  }
  if (support === "unsupported") {
    return citations.length > 0
      ? {
          text: `Couldn't confirm: ${count}`,
          why: `Loom found none of the sources this answer cites ${IN_LOOM}; each source below says why.`,
          icon: "warning",
          tone: "text-warn",
        }
      : {
          text: "Couldn't confirm: it gives no sources",
          why: "This answer looked in Loom but gives no source for what it says, so Loom had nothing to compare it with.",
          icon: "warning",
          tone: "text-warn",
        };
  }
  // "unchecked".
  return citations.length > 0
    ? {
        text: "Sources not checked",
        why: "Loom could not check this answer's sources; each source below says why.",
        icon: "info-in-circle",
        tone: "text-text-muted",
      }
    : {
        text: "Not checked: it cites nothing Loom holds",
        why: "This answer cites nothing: what it read has no rows an answer can cite, a read failed or was refused, or its reads could not be checked.",
        icon: "info-in-circle",
        tone: "text-text-muted",
      };
}

/**
 * One finished answer's label and citations, as the server checked them against Loom with
 * the answer's token. The label follows the answer's `support` (server/assistant/domain/
 * support.py): an answer that makes no claim, such as a greeting or an abstention, has none.
 * A memory, document or help page found in Loom opens where the dashboard shows it; a
 * citation not found says why.
 */
export function Sources({
  citations,
  support,
  unknownNames = [],
  answer,
  askAgain,
  blocked = null,
  checkLost = false,
  numbers,
  idPrefix,
}: {
  citations: Citation[];
  /** Absent: none came (a server that sends none, or a check that never came back). */
  support?: Support;
  /** The check of this answer's sources never finished: with no support, NOT_CHECKED. */
  checkLost?: boolean;
  unknownNames?: string[];
  answer: number;
  /** "Ask again with sources", offered on an unsupported label. */
  askAgain?: () => void;
  /** Why nothing can be asked now: "Ask again with sources" shows, unavailable. */
  blocked?: string | null;
  /** The number a citation's marker shows in a preview that numbers them (the search's
   *  card): its row leads with it, at the id `${idPrefix}-${n}`. */
  numbers?: ReadonlyMap<string, number>;
  idPrefix?: string;
}) {
  const inspect = useStore((s) => s.inspect);
  const openDocument = useStore((s) => s.openDocument);
  const openDocsPage = useStore((s) => s.openDocsPage);
  const labelId = useId();
  const listId = useId();
  const label = support ? supportLabel(support, citations, unknownNames) : checkLost ? NOT_CHECKED : null;
  // Where a citation found in Loom opens: a memory in the drawer (any writer's: the wildcard
  // reads it whichever agent, user or session it is filed under), a document in its
  // preview, a docs page on the Help view at its section.
  const opener = (c: Citation): (() => void) | undefined => {
    if (!c.verified || !c.ref) return undefined;
    if (c.kind === "memory") return () => inspect(c.ref, { agent: "*" });
    if (c.kind === "doc") return () => openDocument(c.ref);
    if (c.kind === "help") return () => openDocsPage(c.ref, c.anchor);
    return undefined;
  };
  // The rows: in the order their numbers run where a preview numbers them, each named as
  // the reader knows it; two memory rows that would read the same show their versions.
  const named = citations.map((c) => ({ c, ...sourceName(c) }));
  const count = new Map<string, number>();
  for (const r of named) count.set(r.name, (count.get(r.name) ?? 0) + 1);
  const numberOf = (c: Citation) => numbers?.get(citationKey(c.text));
  const rank = (c: Citation) => numberOf(c) ?? Number.POSITIVE_INFINITY;
  const rows = named
    .map((r) => (r.versioned && (count.get(r.name) ?? 0) > 1 ? { ...r, name: r.versioned } : r))
    .sort((a, b) => rank(a.c) - rank(b.c));
  if (!label && citations.length === 0) return null;
  return (
    <div className="mt-2 space-y-1" data-testid="assistant-sources">
      {label && (
        <div className={`flex flex-wrap items-center gap-1 text-xs ${label.tone}`} data-testid="assistant-support">
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
          {askAgain && support === "unsupported" && (
            <Unavailable reason={blocked}>
              <Button type="secondary" htmlType="button" className="ml-1" onClick={askAgain}>
                Ask again with sources
              </Button>
            </Unavailable>
          )}
        </div>
      )}
      {citations.length > 0 && (
        <>
          {/* One label over the rows: a row is its source as the reader knows it. */}
          <p id={listId} className="section-label">
            Sources
          </p>
          <ul className="space-y-1 text-xs" aria-labelledby={listId}>
            {rows.map(({ c, name, detail }) => {
              const open = opener(c);
              const n = numberOf(c);
              return (
                <li key={c.text} id={n && idPrefix ? `${idPrefix}-${n}` : undefined} className="source-row break-words">
                  {n !== undefined && <span className="text-text-muted">{`${n}. `}</span>}
                  {open ? (
                    <Tip content={detail} disabled={!detail}>
                      <Link component="button" type="button" size="sm" onClick={open}>
                        {name}
                      </Link>
                    </Tip>
                  ) : (
                    // A tooltip only where it adds something (a kind, a version), and with
                    // it the tab stop that reaches it.
                    <TipTarget tip={detail} disabled={!detail}>
                      <span className={c.verified ? "text-text" : "text-text-muted"}>{name}</span>
                    </TipTarget>
                  )}
                  {!c.verified && (
                    <span className="text-text-muted">{` · not confirmed${c.reason ? ` (${c.reason})` : ""}`}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}

const KINDS: Record<string, string> = {
  memory: "Memory",
  doc: "Document",
  help: "Help page",
  code: "Code",
};

/** A citation as the reader knows it: a memory's key, a document's address and section, a
 *  help page, a code path, a SQL query by its first line. `detail` (its tooltip) adds the
 *  kind and a memory's version, null where it would add nothing; `versioned`, a memory's
 *  key with its version, tells two rows of one memory apart. */
export function sourceName(c: Citation): { name: string; detail: string | null; versioned?: string } {
  if (c.kind === "sql") {
    // The statement's first line tells one query of the answer from another.
    const first =
      c.text
        .replace(/^```sql\s*/i, "")
        .trim()
        .split("\n")[0] ?? "";
    const name = `SQL query: ${first.length > 80 ? `${first.slice(0, 79)}…` : first}`;
    return { name, detail: null };
  }
  if (!new RegExp(`^${CITATION.source}$`).test(c.text)) return { name: c.text, detail: null };
  const body = c.text.slice(1, -1);
  const kind = body.slice(0, body.indexOf(":"));
  const unkinded = body.slice(kind.length + 1);
  const version = kind === "memory" ? /@v(\d+)$/.exec(unkinded)?.[1] : undefined;
  const name = version ? unkinded.slice(0, -(version.length + 2)) : unkinded;
  const detail = version ? `${KINDS[kind]} ${name}, version ${version}` : `${KINDS[kind]} ${name}`;
  return version ? { name, detail, versioned: `${name}, version ${version}` } : { name, detail };
}
