import { fmtInt } from "../../lib/format";
import { Badge } from "@clickhouse/click-ui/Badge";
import type { InspectCount, InspectView, MemoryInspection } from "../../api";
import { KeyButton } from "../../components/ui/KeyButton";
import { CodeBlock } from "./CodeBlock";
import { ExactNames } from "./ExactNames";
import { ListTime } from "../../components/ui/ListTime";
import { SectionHeading } from "../../components/ui";
import { useStore } from "../../lib/state";
import { SUBJECT_COPY, shownName } from "./subjects";

// What the subject's own view holds, as the server defines it: rows filed under a user
// (by any writer), rows an agent wrote, rows written in a session (by any user).
const OWN_HEADING = {
  user: "Filed under this user",
  agent: "Written by this agent",
  session: "Written in this session",
} as const;
// server/memory/app/inspect.py TOP_TAGS: a view lists at most this many tags.
const TOP_TAGS = 20;

/** What one user, agent or session remembers: the subject's own rows as the server defines
 *  them, apart from every other live row, and what creates them. */
export function Inspection({ result }: { result: MemoryInspection }) {
  // A section the server could not read is named in `skipped` as `<section>: <error>`;
  // its fields then hold defaults, not answers.
  const unread = (section: string) => result.skipped.some((s) => s.startsWith(`${section}:`));
  return (
    <div className="space-y-5" data-testid="assistant-inspection">
      <p className="text-sm text-text">
        {SUBJECT_COPY[result.subject].label}{" "}
        <span className="font-mono break-all">{shownName(result.subject, result.id)}</span>
      </p>
      {/* Both views' definitions mean the same names: shown once. */}
      <ExactNames refs={[...new Set([...(result.own.refs ?? []), ...(result.others.refs ?? [])])]} />
      <InspectViewBlock
        id="inspect-own"
        title={OWN_HEADING[result.subject]}
        view={result.own}
        unread={(part) => unread(`own.${part}`)}
      />
      <InspectViewBlock
        id="inspect-others"
        title="Every other live memory of the namespace"
        view={result.others}
        unread={(part) => unread(`others.${part}`)}
      />
      <section aria-labelledby="inspect-settings" className="space-y-1 text-sm">
        <SectionHeading id="inspect-settings" level={3} label="What creates these memories" />
        <p className="text-text">
          {unread("settings.guide") ? (
            <span className="text-text-muted">The guide could not be read.</span>
          ) : (
            <>
              {result.settings.guide_revision ? `Guide revision ${result.settings.guide_revision}` : "No guide written"}
              {result.settings.guide_summary && ` · ${result.settings.guide_summary}`}
            </>
          )}
        </p>
        <p className="text-text">
          {unread("settings.categories") ? (
            <span className="text-text-muted">The registered categories could not be read.</span>
          ) : (
            `Categories registered: ${fmtInt(result.settings.categories_registered)}`
          )}
        </p>
        <div className="text-text" data-testid="inspect-policy">
          Operator extraction policy:{" "}
          {result.settings.extraction_policy === null ? (
            <span className="text-text-muted">
              not shown
              {result.settings.extraction_policy_unavailable && ` (${result.settings.extraction_policy_unavailable})`}
            </span>
          ) : result.settings.extraction_policy === "" ? (
            <span className="text-text-muted">none set</span>
          ) : (
            <CodeBlock testId="inspect-policy-text">{result.settings.extraction_policy}</CodeBlock>
          )}
        </div>
        <p className="text-text-muted">{result.settings.extraction_instructions}</p>
        <ExactNames refs={result.settings.extraction_instructions_refs} />
      </section>
      <section aria-labelledby="inspect-activity" className="space-y-1 text-sm">
        <SectionHeading id="inspect-activity" level={3} label="Activity" />
        {unread("activity") ? (
          <p className="text-text-muted">The calls could not be read.</p>
        ) : result.activity.calls.length === 0 ? (
          <p className="text-text-muted">No calls in the last {windowText(result.activity.window_seconds)}.</p>
        ) : (
          <p className="text-text tabular">
            Last {windowText(result.activity.window_seconds)}, succeeded of called:{" "}
            {result.activity.calls.map((c) => `${c.operation} ${fmtInt(c.succeeded)}/${fmtInt(c.calls)}`).join(" · ")}
          </p>
        )}
        {result.activity.gap && <p className="text-xs text-text-muted">{result.activity.gap}</p>}
        {unread("activity.retention") && (
          <p className="text-xs text-text-muted">
            How long calls are kept could not be read, so older calls may be missing from the window.
          </p>
        )}
      </section>
      {result.skipped.length > 0 && (
        <p className="text-xs text-text-muted" data-testid="inspect-skipped">
          Not read: {result.skipped.join("; ")}
        </p>
      )}
    </div>
  );
}

/** The window in whole days, else in hours, rounded; the page sends the 7-day default. */
function windowText(seconds: number): string {
  const [n, unit] = seconds % 86_400 === 0 ? [seconds / 86_400, "day"] : [Math.round(seconds / 3600), "hour"];
  return `${fmtInt(n)} ${unit}${n === 1 ? "" : "s"}`;
}

function counts(label: string, items: InspectCount[]): string | null {
  if (items.length === 0) return null;
  return `${label}: ${items.map((c) => `${c.name} ${fmtInt(c.rows)}`).join(", ")}`;
}

/** One view. A part the server could not read (`<view>.<part>` in `skipped`) is said to be
 *  unread: unknown, not none. */
function InspectViewBlock({
  id,
  title,
  view,
  unread,
}: {
  id: string;
  title: string;
  view: InspectView;
  unread: (part: "counts" | "newest" | "tags" | "categories") => boolean;
}) {
  const counted = !unread("counts");
  const listed = !unread("newest");
  const inspect = useStore((s) => s.inspect);
  // Types, statuses and sources come from the counts; tags, categories and the listing
  // are read apart from them, so a failed count hides only what it would have said.
  const lines = [
    counted && counts("Types", view.by_type),
    counted && counts("Statuses", view.by_status),
    counted && counts("Sources", view.by_source_class),
    unread("tags")
      ? "Tags: could not be read"
      : counts(view.top_tags.length >= TOP_TAGS ? `Top ${TOP_TAGS} tags` : "Tags", view.top_tags),
    unread("categories") ? "Categories: could not be read" : counts("Categories", view.categories),
  ].filter((l): l is string => Boolean(l));
  return (
    <section aria-labelledby={id} className="space-y-2" data-view={id}>
      <SectionHeading id={id} level={3} label={title} count={counted ? view.live_rows : undefined} />
      <p className="text-xs text-text-muted">{view.definition}</p>
      {!counted && <p className="text-sm text-text-muted">The memories could not be counted.</p>}
      {counted && view.live_rows === 0 ? (
        <p className="text-sm text-text-muted">No live memories.</p>
      ) : (
        <>
          {lines.map((l) => (
            <p key={l} className="text-xs text-text-muted">
              {l}
            </p>
          ))}
          {!listed && <p className="text-xs text-text-muted">The memories could not be listed.</p>}
          {listed && counted && view.newest.length < view.live_rows && (
            <p className="text-xs text-text-muted">
              Newest {fmtInt(view.newest.length)} of {fmtInt(view.live_rows)} listed.
            </p>
          )}
          {view.newest.length > 0 && (
            <ul className="space-y-1.5" aria-label={`${title}, newest first`}>
              {view.newest.map((r) => (
                <li key={r.memory_id} className="text-sm">
                  <div className="flex flex-wrap items-center gap-x-2">
                    {/* Opened under its whole writer identity: memory.get reads a row by it. */}
                    <KeyButton
                      memoryKey={r.memory_key}
                      onInspect={(k) => inspect(k, { agent: r.agent, userId: r.user_id, sessionId: r.session_id })}
                    />
                    {/* The version is a nanosecond stamp a JavaScript number cannot hold
                        exactly; the count of versions is what a reader compares. */}
                    <span className="text-xs text-text-muted tabular">
                      {fmtInt(r.versions)} version{r.versions === 1 ? "" : "s"}
                    </span>
                    {r.sensitivity === "high" && <Badge text="high sensitivity" state="neutral" size="sm" />}
                    <ListTime time={r.updated_at} />
                  </div>
                  {r.excerpt && <p className="text-text-muted break-words">{r.excerpt}</p>}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
