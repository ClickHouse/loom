import { useEffect, useState } from "react";
import { BookMarked, ChevronDown, ChevronRight } from "lucide-react";
import { getBrief, type BriefGetResponse } from "../api/briefs";
import { CopyButton } from "./CopyButton";
import { humanizeError, relativeTime } from "../lib/format";

/** What to say in Claude Code or Codex (the loom-session skill) to restore a
 *  brief: the brief with the client's compaction summary, or, asked for
 *  explicitly, with every recorded turn as well. */
export function restorePhrase(key: string, depth: "restore" | "transcript" = "restore"): string {
  if (depth === "transcript") return `Restore the session brief ${key} with its full transcript.`;
  return `Restore the session brief ${key}.`;
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
    if (!open || !head) return;
    if (revision === 0) {
      setBody(head);
      return;
    }
    let live = true;
    setError(null);
    getBrief({ org, namespace }, head.brief_key, { revision, full: true })
      .then((b) => live && setBody(b.found ? b : null))
      .catch((e) => live && setError(humanizeError(e)));
    return () => {
      live = false;
    };
  }, [open, head, revision, org, namespace]);

  if (!head) return null;
  const Toggle = open ? ChevronDown : ChevronRight;
  return (
    <div className="mb-4 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)]">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left text-xs"
      >
        <Toggle size={14} className="shrink-0 text-[var(--color-text-muted)]" />
        <BookMarked size={14} className="shrink-0 text-[var(--color-text-muted)]" />
        <span className="font-medium">Session brief</span>
        <span className="font-mono text-[var(--color-text-muted)] truncate">{head.brief_key}</span>
        <span className="ml-auto shrink-0 text-2xs text-[var(--color-text-muted)]">
          rev {head.revision}
          {head.updated_at ? ` · saved ${relativeTime(head.updated_at)}` : ""}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-2">
          {head.revisions > 1 && (
            <div className="flex items-center gap-2">
              <select
                aria-label="Revision"
                value={revision}
                onChange={(e) => setRevision(Number(e.target.value))}
                className="ml-auto px-2 py-1 text-xs rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text)]"
              >
                <option value={0}>latest (rev {head.revision})</option>
                {Array.from({ length: head.revisions }, (_, i) => head.revisions - i).map((r) => (
                  <option key={r} value={r}>
                    rev {r}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div className="flex items-center gap-2 rounded-md bg-[var(--color-surface)] border border-[var(--color-border)] px-2 py-1.5">
            <span className="text-2xs text-[var(--color-text-muted)] shrink-0">Say in Claude Code or Codex:</span>
            <code className="text-2xs flex-1 min-w-0 break-all">{restorePhrase(head.brief_key)}</code>
            <CopyButton
              text={() => restorePhrase(head.brief_key)}
              label="Copy"
              ariaLabel="Copy the restore phrase"
              className="shrink-0 inline-flex items-center gap-1 px-2 py-0.5 text-2xs font-medium bg-[var(--color-surface)] border border-[var(--color-border)] rounded-md hover:bg-[var(--color-surface-3)]"
            />
          </div>
          {error && <div className="text-xs text-danger">{error}</div>}
          {body && (
            <pre className="max-h-[40vh] overflow-auto text-2xs whitespace-pre-wrap break-words font-mono leading-relaxed">
              {body.rendered}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
