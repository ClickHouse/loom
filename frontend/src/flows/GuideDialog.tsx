import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  GUIDE_HISTORY_LIMIT,
  getNamespaceGuide,
  guideHistory,
  saveNamespaceGuide,
  type GuideRevision,
  type NamespaceGuide,
} from "../api";
import { toast, toastError } from "../components/Toast";
import { Alert, Loading, Unavailable } from "../components/ui";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { relativeTime } from "../lib/format";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  namespace: string;
  /** A writer or admin edits; everyone else reads. */
  canEdit: boolean;
  /** Open on the editor when the guide's read finds none and the caller can edit (the
   *  Memories header); otherwise on the read view. */
  startEditing?: boolean;
  onSaved: () => void;
}

/** A first guide starts from the questions a newcomer to the namespace asks. */
const STARTER = `## What this namespace holds

## Keys
- \`<prefix>/<id>\`: …

## Tags
- \`<tag>\`: what it marks

## How to use it
- …
`;

/**
 * A namespace's guide: what it holds and how to use it, the text agents read at session
 * start. Editing works like an agent's save: it names the revision it started from, and
 * when someone saved in between, their text is shown beside the draft to merge before
 * saving again on top of it.
 */
export function GuideDialog({ open, onOpenChange, org, namespace, canEdit, startEditing, onSaved }: Props) {
  const [guide, setGuide] = useState<NamespaceGuide | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState("");
  const [summary, setSummary] = useState("");
  const [note, setNote] = useState("");
  // The guide the draft is saved on top of; after a conflict, the one that refused it.
  const [base, setBase] = useState<NamespaceGuide | null>(null);
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);

  useAbortableEffect(
    async (signal, live) => {
      try {
        const g = await getNamespaceGuide(org, namespace, { signal });
        if (!live()) return;
        setGuide(g);
        // Someone may have written one since the caller looked: then it is read first.
        if (startEditing && canEdit && !g.found) startEdit(g);
      } catch (e) {
        if (live()) setError(humanizeError(e));
      }
    },
    [org, namespace],
  );

  function startEdit(g: NamespaceGuide) {
    setContent(g.found ? g.content : STARTER);
    setSummary(g.summary);
    setNote("");
    setBase(g);
    setConflict(false);
    setEditing(true);
  }

  // The outline as given, give or take whitespace anywhere, is not a guide yet.
  const untouched = content.replace(/\s+/g, "") === STARTER.replace(/\s+/g, "");

  const save = async () => {
    if (!base || untouched) return;
    setBusy(true);
    try {
      // A summary left as the saved one is not sent, so the server keeps whatever
      // is current: a save after a conflict does not put back an older summary.
      const r = await saveNamespaceGuide(org, namespace, {
        content,
        ...(summary !== base.summary ? { summary } : {}),
        note,
        expected_revision: base.revision,
      });
      if (r.conflict && r.current) {
        if (summary === base.summary) setSummary(r.current.summary);
        setBase(r.current);
        setConflict(true);
        return;
      }
      toast(r.written ? `Saved revision ${r.revision}` : "No change to save", "ok");
      onSaved();
      onOpenChange(false);
    } catch (e) {
      toastError("Save failed", e);
    } finally {
      setBusy(false);
    }
  };

  const close = (v: boolean) => {
    if (!busy) onOpenChange(v);
  };
  return (
    <Modal
      open={open}
      onOpenChange={close}
      title={`Guide for ${namespace}`}
      description="What this namespace holds and how to use it. Agents read it when a session starts."
      className="w-full max-w-[760px]"
    >
      {error !== null && <Alert role="alert" state="danger" text={error} />}
      {guide === null && error === null && <Loading label="Loading the guide" />}
      {guide !== null && !editing && (
        <div className="space-y-3">
          {guide.found ? (
            <>
              <p className="text-xs text-text-muted">
                {byline(guide)}
                {guide.note && <span className="text-text"> — {guide.note}</span>}
              </p>
              {guide.summary && <p className="text-sm text-text">{guide.summary}</p>}
              <pre className="max-h-[50vh] overflow-auto whitespace-pre-wrap break-words rounded border border-border p-3 font-mono text-xs text-text">
                {guide.content}
              </pre>
              {guide.revision > 1 && <GuideHistory org={org} namespace={namespace} current={guide.revision} />}
            </>
          ) : (
            <p className="text-sm text-text-muted">
              No guide yet. Agents that write here follow it once one exists: the key layout, what each tag means and
              which rows to ignore.
            </p>
          )}
          <ModalFooter>
            <Button type="secondary" htmlType="button" onClick={() => close(false)}>
              Close
            </Button>
            {canEdit && (
              <Button type="primary" htmlType="button" onClick={() => startEdit(guide)}>
                {guide.found ? "Edit" : "Write the guide"}
              </Button>
            )}
          </ModalFooter>
        </div>
      )}
      {editing && base !== null && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <div className="space-y-4">
            {conflict && (
              <>
                <Alert
                  role="alert"
                  state="warning"
                  text={`${byline(base)} was saved while you edited. Merge it into your text, then save again.`}
                />
                {base.summary && <p className="text-sm text-text">Their summary: {base.summary}</p>}
                <pre className="max-h-[30vh] overflow-auto whitespace-pre-wrap break-words rounded border border-border p-3 font-mono text-xs text-text">
                  {base.content}
                </pre>
              </>
            )}
            <TextField
              id="guide-summary"
              label="Summary (one line, shown in the namespace list)"
              value={summary}
              onChange={setSummary}
              maxLength={300}
            />
            <TextAreaField
              id="guide-content"
              label="Guide (Markdown)"
              value={content}
              onChange={setContent}
              rows={16}
              maxLength={32000}
            />
            <TextField id="guide-note" label="What changed" value={note} onChange={setNote} maxLength={500} />
          </div>
          <ModalFooter>
            <Button type="secondary" htmlType="button" disabled={busy} onClick={() => close(false)}>
              Cancel
            </Button>
            {/* The outline is questions to answer: saved as it is, every agent session would
                start with it. */}
            <Unavailable reason={untouched ? "Fill in the outline first" : null}>
              <Button type="primary" htmlType="submit" loading={busy} disabled={busy}>
                Save
              </Button>
            </Unavailable>
          </ModalFooter>
        </form>
      )}
    </Modal>
  );
}

/** "Revision 3 by …, 2 days ago": who wrote a revision and when, each part when known. */
function byline(g: { revision: number; updated_by: string; updated_at: string | null }): string {
  const by = g.updated_by ? ` by ${g.updated_by}` : "";
  return `Revision ${g.revision}${by}${g.updated_at ? `, ${relativeTime(g.updated_at)}` : ""}`;
}

/** The guide's earlier revisions, read when opened: who changed it, when and why. The
 *  newest one is the guide shown above, so the list starts below it. */
function GuideHistory({ org, namespace, current }: { org: string; namespace: string; current: number }) {
  const [open, setOpen] = useState(false);
  const [revisions, setRevisions] = useState<GuideRevision[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reads, setReads] = useState(0);
  useAbortableEffect(
    (signal, live) => {
      if (!open) return;
      setError(null);
      guideHistory(org, namespace, GUIDE_HISTORY_LIMIT, { signal })
        .then((r) => live() && setRevisions(r))
        .catch((e) => live() && setError(humanizeError(e)));
    },
    [open, reads, org, namespace],
  );
  const earlier = revisions?.filter((r) => r.revision !== current) ?? null;
  return (
    <section>
      <Button
        type="empty"
        htmlType="button"
        iconLeft={open ? "chevron-down" : "chevron-right"}
        aria-expanded={open}
        aria-controls="guide-history"
        onClick={() => setOpen((o) => !o)}
      >
        History
      </Button>
      {open && (
        <div id="guide-history" className="mt-1">
          {error && (
            <div className="flex items-center gap-2">
              <Alert role="alert" state="danger" text={error} />
              <Button type="secondary" htmlType="button" onClick={() => setReads((n) => n + 1)}>
                Retry
              </Button>
            </div>
          )}
          {!error && earlier === null && <Loading label="Loading the history" />}
          {!error && earlier && (
            <ul className="space-y-1 text-xs">
              {earlier.map((r) => (
                <li key={r.revision}>
                  <span className="text-text-muted">{byline(r)}</span>
                  {r.note && <span className="text-text"> — {r.note}</span>}
                </li>
              ))}
            </ul>
          )}
          {!error && revisions?.length === GUIDE_HISTORY_LIMIT && (
            <p className="mt-1 text-xs text-text-muted">Only the newest {GUIDE_HISTORY_LIMIT} revisions are read.</p>
          )}
        </div>
      )}
    </section>
  );
}
