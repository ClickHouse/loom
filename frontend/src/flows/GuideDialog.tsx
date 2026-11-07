import { useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { getNamespaceGuide, guideHistory, saveNamespaceGuide, type GuideRevision, type NamespaceGuide } from "../api";
import { toast } from "../components/Toast";
import { Alert, Loading, Unavailable } from "../components/ui";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { humanizeError, relativeTime } from "../lib/format";
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
      toast(`Save failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const close = (v: boolean) => {
    if (!busy) onOpenChange(v);
  };
  const byline = (g: NamespaceGuide) =>
    `Revision ${g.revision}${g.updated_by ? ` by ${g.updated_by}` : ""}${g.updated_at ? `, ${relativeTime(g.updated_at)}` : ""}`;

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
              <p className="text-xs text-text-muted">{byline(guide)}</p>
              {guide.summary && <p className="text-sm text-text">{guide.summary}</p>}
              <pre className="max-h-[50vh] overflow-auto whitespace-pre-wrap break-words rounded border border-border p-3 font-mono text-xs text-text">
                {guide.content}
              </pre>
              {guide.revision > 1 && <GuideHistory org={org} namespace={namespace} />}
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

/** The guide's earlier revisions, read when opened: who changed it, when and why. */
function GuideHistory({ org, namespace }: { org: string; namespace: string }) {
  const [open, setOpen] = useState(false);
  const [revisions, setRevisions] = useState<GuideRevision[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open || revisions !== null) return;
    let live = true;
    guideHistory(org, namespace)
      .then((r) => live && setRevisions(r))
      .catch((e) => live && setError(humanizeError(e)));
    return () => {
      live = false;
    };
  }, [open, revisions, org, namespace]);
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
          {error && <Alert role="alert" state="danger" text={error} />}
          {!error && revisions === null && <Loading label="Loading the history" />}
          <ul className="space-y-1 text-xs">
            {revisions?.map((r) => (
              <li key={r.revision}>
                <span className="text-text-muted">
                  Revision {r.revision}
                  {r.updated_by ? ` by ${r.updated_by}` : ""}, {relativeTime(r.updated_at)}
                </span>
                {(r.note || r.summary) && <span className="text-text"> — {r.note || r.summary}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
