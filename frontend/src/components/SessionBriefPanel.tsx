import { useEffect, useState } from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import { getBrief, type BriefGetResponse } from "../api/briefs";
import { CopyButton } from "./CopyButton";
import { briefHandoffUrl } from "./SessionHandoff";
import { relativeTime } from "../lib/format";
import { humanizeError } from "../lib/errorWords";

/** What to say in Claude Code or Codex (the loom-session skill) to restore a
 *  brief: the brief with the client's compaction summary, or, asked for
 *  explicitly, with every recorded turn as well. */
export function restorePhrase(key: string, depth: "restore" | "transcript" = "restore", revision = 0): string {
  const target = revision ? `revision ${revision} of the session brief ${key}` : `the session brief ${key}`;
  return `Restore ${target}${depth === "transcript" ? " with its full transcript" : ""}.`;
}

/**
 * The session brief a coding session left, shown inside that session's
 * conversation. Renders nothing when the session left none. Collapsed until
 * opened: restoring is something the person does in Claude Code, on request;
 * this is where they read what would be restored and copy the words for it.
 */
export function SessionBriefPanel({
  org,
  namespace,
  sessionId = "",
  briefKey = "",
}: {
  org: string;
  namespace: string;
  /** Look the brief up by the session that wrote it… */
  sessionId?: string;
  /** …or by its key. */
  briefKey?: string;
}) {
  const [head, setHead] = useState<BriefGetResponse | null>(null);
  const [open, setOpen] = useState(false);
  const [revision, setRevision] = useState(0);
  const [body, setBody] = useState<BriefGetResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Is there a brief for this session at all?
  useEffect(() => {
    setHead(null);
    setOpen(false);
    setRevision(0);
    setBody(null);
    if (!sessionId && !briefKey) return;
    let live = true;
    getBrief({ org, namespace }, briefKey, { fromSession: sessionId, full: true })
      .then((b) => live && setHead(b.found ? b : null))
      .catch(() => live && setHead(null));
    return () => {
      live = false;
    };
  }, [org, namespace, sessionId, briefKey]);

  // The text at the chosen revision, once opened: the brief with the client's
  // compaction summary, exactly what a restore loads.
  useEffect(() => {
    if (!open || !head || revision === 0) return;
    let live = true;
    setError(null);
    getBrief({ org, namespace }, head.brief_key, { revision, full: true })
      .then((b) => live && setBody(b.found ? b : null))
      .catch((e) => live && setError(humanizeError(e, { read: true })));
    return () => {
      live = false;
    };
  }, [open, head, revision, org, namespace]);

  if (!head) return null;
  const shown = revision ? (body?.revision === revision ? body : null) : head;
  const handoff = shown && briefHandoffUrl({ org, namespace }, shown);
  const prompt = shown && restorePhrase(shown.brief_key, "restore", revision);
  return (
    <div className="mb-4 rounded-sm bg-surface-2 border border-border">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left text-xs"
      >
        <Icon name={open ? "chevron-down" : "chevron-right"} size="sm" aria-hidden="true" />
        <Icon name="book" size="sm" aria-hidden="true" />
        <span className="font-medium">Session brief</span>
        <span className="font-mono text-text-muted truncate">{head.brief_key}</span>
        <span className="ml-auto shrink-0 text-xs text-text-muted">
          rev {head.revision}
          {head.updated_at ? ` · saved ${relativeTime(head.updated_at)}` : ""}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-2">
          {head.revisions > 1 && (
            <div className="flex justify-end">
              <Select
                label="Revision"
                value={String(revision)}
                onSelect={(value) => {
                  setBody(null);
                  setError(null);
                  setRevision(Number(value));
                }}
              >
                <Select.Item value="0">latest (rev {head.revision})</Select.Item>
                {Array.from({ length: head.revisions }, (_, i) => head.revisions - i).map((r) => (
                  <Select.Item key={r} value={String(r)}>
                    rev {r}
                  </Select.Item>
                ))}
              </Select>
            </div>
          )}
          {prompt && (
            <div className="flex flex-wrap items-center gap-2 rounded-sm bg-surface border border-border px-2 py-1.5">
              <span className="text-xs text-text-muted shrink-0">Say in Claude Code or Codex:</span>
              <code className="text-xs flex-1 min-w-0 break-all">{prompt}</code>
              <CopyButton text={prompt} label="Copy" ariaLabel="Copy the restore phrase" className="shrink-0" />
            </div>
          )}
          {handoff && <CopyButton text={handoff} label="Copy handoff link" className="shrink-0" />}
          {error && <div className="text-xs text-danger">{error}</div>}
          {shown && (
            <pre className="max-h-[40vh] overflow-auto text-xs whitespace-pre-wrap break-words font-mono">
              {shown.rendered}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
