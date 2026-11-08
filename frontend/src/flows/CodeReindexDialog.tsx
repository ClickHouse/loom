import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import { codeReindex, type CodeRepoSummary } from "../api";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert } from "../components/ui";

/** A maintainer's repair after a bad ingest pass: move the branch's watermark back to a
 *  commit (code.reindex), and the ingest worker walks again from there. */
export function CodeReindexDialog({
  branch,
  onDone,
  onClose,
}: {
  branch: CodeRepoSummary;
  onDone: () => void;
  onClose: () => void;
}) {
  const [sha, setSha] = useState("");
  // The server moves the watermark only to a full commit id.
  const full = /^[0-9a-f]{40}$/.test(sha.trim().toLowerCase());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await codeReindex({ org: branch.org, namespace: branch.namespace }, sha.trim().toLowerCase());
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
          <TextField
            id="reindex-sha"
            label="Commit"
            value={sha}
            onChange={setSha}
            placeholder="The full 40-character commit id"
            error={sha.trim() && !full ? "A full 40-character commit id" : undefined}
          />
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={busy || !full} loading={busy}>
            Re-walk
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
