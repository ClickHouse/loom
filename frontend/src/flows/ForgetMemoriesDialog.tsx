import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import { clearAgentMemories } from "../api";
import { useIdentity } from "../lib/state";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert } from "../components/ui";

/** Tombstone every memory one agent holds in the current namespace (memory.clear). The
 *  agent's name is typed again to confirm, as the server asks. */
export function ForgetMemoriesDialog({ agent, onClose }: { agent: string; onClose: () => void }) {
  const identity = useIdentity();
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = typed === agent && !busy;

  const submit = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      const r = await clearAgentMemories(identity, agent);
      const locked = r.locked_skipped > 0 ? `; ${r.locked_skipped} locked kept` : "";
      toast(`Forgot ${r.total} rows for ${agent}${locked}`, "ok");
      onClose();
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  return (
    <Modal open onOpenChange={(v) => !v && onClose()} title="Forget memories" className="w-full max-w-[520px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-4">
          <p className="text-sm text-text-muted leading-relaxed">
            Deletes every memory the agent <span className="font-mono text-text">{agent}</span> holds in{" "}
            <span className="font-mono text-text">{identity.namespace}</span>. Locked memories stay. Deleted memories
            can be restored for 14 days from Memories → Recently deleted.
          </p>
          <TextField id="forget-confirm" label={`Type ${agent} to confirm`} value={typed} onChange={setTyped} />
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="danger" htmlType="submit" disabled={!ready} loading={busy}>
            Forget memories
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
