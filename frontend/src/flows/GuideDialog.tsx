import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { getNamespaceGuide, saveNamespaceGuide, type NamespaceGuide } from "../api";
import { toast } from "../components/Toast";
import { Alert, Loading } from "../components/ui";
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
  onSaved: () => void;
}

/**
 * A namespace's guide: what it holds and how to use it, the text agents read at session
 * start. Editing works like an agent's save: it names the revision it started from, and
 * when someone saved in between, their text is shown beside the draft to merge before
 * saving again on top of it.
 */
export function GuideDialog({ open, onOpenChange, org, namespace, canEdit, onSaved }: Props) {
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
        if (live()) setGuide(g);
      } catch (e) {
        if (live()) setError(humanizeError(e));
      }
    },
    [org, namespace],
  );

  const startEdit = (g: NamespaceGuide) => {
    setContent(g.content);
    setSummary(g.summary);
    setNote("");
    setBase(g);
    setConflict(false);
    setEditing(true);
  };

  const save = async () => {
    if (!base) return;
    setBusy(true);
    try {
      const r = await saveNamespaceGuide(org, namespace, {
        content,
        summary,
        note,
        expected_revision: base.revision,
      });
      if (r.conflict && r.current) {
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
              placeholder={"# What this namespace holds\n\nKeys: …\nTags: …"}
            />
            <TextField id="guide-note" label="What changed" value={note} onChange={setNote} maxLength={500} />
          </div>
          <ModalFooter>
            <Button type="secondary" htmlType="button" disabled={busy} onClick={() => close(false)}>
              Cancel
            </Button>
            <Button type="primary" htmlType="submit" loading={busy} disabled={busy}>
              Save
            </Button>
          </ModalFooter>
        </form>
      )}
    </Modal>
  );
}
