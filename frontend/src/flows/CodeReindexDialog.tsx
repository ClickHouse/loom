import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { codeRecentCommits, codeReindex, type CodeRecentCommitRow, type CodeRepoSummary } from "../api";
import { humanizeError } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, Loading } from "../components/ui";

/** A maintainer's repair after a bad ingest pass: move the branch's watermark back to a
 *  commit it indexed (code.reindex), and the ingest worker walks again from there. The
 *  commit is picked from the branch's own, since the server moves the watermark to any id
 *  and one not on the branch stops its ingest. */
export function CodeReindexDialog({
  branch,
  onDone,
  onClose,
}: {
  branch: CodeRepoSummary;
  onDone: () => void;
  onClose: () => void;
}) {
  const id = { org: branch.org, namespace: branch.namespace };
  const [commits, setCommits] = useState<CodeRecentCommitRow[] | null>(null);
  const [sha, setSha] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useAbortableEffect(
    async (signal, live) => {
      try {
        const r = await codeRecentCommits(id, 100, { signal });
        if (live()) setCommits(r.rows);
      } catch (e) {
        if (live()) setError(humanizeError(e));
      }
    },
    [branch.org, branch.namespace],
  );

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await codeReindex(id, sha);
      toast(`${branch.repo} @ ${branch.branch} walks again from ${r.new_head.slice(0, 12)}`, "ok");
      onDone();
      onClose();
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };
  return (
    <Modal open onOpenChange={(v) => !v && onClose()} title="Re-walk from a commit" className="w-full max-w-[480px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-4">
          <p className="text-sm text-text-muted">
            The index of {branch.repo} @ {branch.branch} is walked again from this commit. Every write is an upsert, so
            walking again is safe.
          </p>
          {commits === null && !error && <Loading label="Loading the branch's commits" />}
          {commits?.length === 0 && <p className="text-sm text-text-muted">The branch has no indexed commits yet.</p>}
          {commits && commits.length > 0 && (
            <Select
              label="Commit"
              value={sha}
              onSelect={setSha}
              placeholder="One of the branch's indexed commits"
              options={commits.map((c) => ({ value: c.commit, label: `${c.commit.slice(0, 12)} ${c.subject}` }))}
            />
          )}
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={busy || !sha} loading={busy}>
            Re-walk
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
