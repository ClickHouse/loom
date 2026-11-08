import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import { createThreadRow, deleteThread, getConversationThread, renameThread, type Identity } from "../api";
import { humanizeError } from "../lib/format";
import { confirmAction } from "../components/ConfirmDialog";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";
import { Alert } from "../components/ui";

interface Thread {
  thread_id: string;
  agent: string;
  title: string;
}

/** A thread row's menu: Rename (conversation.update, WRITER) and Delete
 *  (conversation.delete, ADMIN), each shown to a role that reaches it. */
export function ThreadActions({
  identity,
  thread,
  canRename,
  canDelete,
  onChanged,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  thread: Thread;
  canRename: boolean;
  canDelete: boolean;
  onChanged: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const name = thread.title || thread.thread_id;

  const remove = async () => {
    const ok = await confirmAction({
      title: `Delete "${name}"?`,
      message: "Its messages leave search and the replay, and the dashboard cannot bring them back.",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      const left = await deleteEveryAgent(identity, thread);
      toast(left ? `Deleted part of "${name}"; ${left} messages are left` : `Deleted "${name}"`, left ? "info" : "ok");
    } catch (e) {
      toast(`Delete failed: ${humanizeError(e)}`, "error");
    }
    onChanged();
  };

  const items: RowMenuItem[] = [
    ...(canRename ? [{ label: "Rename", icon: "pencil" as const, onSelect: () => setRenaming(true) }] : []),
    ...(canDelete ? [{ label: "Delete", icon: "trash" as const, danger: true, onSelect: () => void remove() }] : []),
  ];
  if (items.length === 0) return null;
  return (
    <>
      <RowMenu name={name} items={items} />
      {renaming && (
        <RenameDialog
          identity={identity}
          thread={thread}
          onClose={(renamed) => {
            setRenaming(false);
            if (renamed) onChanged();
          }}
        />
      )}
    </>
  );
}

function RenameDialog({
  identity,
  thread,
  onClose,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  thread: Thread;
  onClose: (renamed: boolean) => void;
}) {
  const [title, setTitle] = useState(thread.title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    try {
      const r = await renameThread(identity, thread.agent, thread.thread_id, title.trim());
      // A thread built by appends has no row to update yet: write one with the title.
      if (!r.updated) await createThreadRow(identity, thread.agent, thread.thread_id, title.trim());
      toast("Renamed", "ok");
      onClose(true);
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };
  return (
    <Modal open onOpenChange={(v) => !v && onClose(false)} title="Rename conversation" className="w-full max-w-[480px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <TextField id="thread-title" label="Title" value={title} onChange={setTitle} />
        {error && <Alert role="alert" state="danger" text={error} />}
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={() => onClose(false)}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={busy || !title.trim()} loading={busy}>
            Rename
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}

// A thread's turns are deleted per agent; one thread is unlikely to hold more agents.
const MAX_AGENTS = 20;

/** Delete each agent's part of the thread, the last writer first, until no turn is left.
 *  Answers how many messages are left when it stopped early. */
async function deleteEveryAgent(id: Pick<Identity, "org" | "namespace">, thread: Thread): Promise<number> {
  let agent = thread.agent;
  let left = 0;
  for (let i = 0; i < MAX_AGENTS; i++) {
    await deleteThread(id, agent, thread.thread_id);
    const rest = await getConversationThread(id, "*", thread.thread_id, { limit: 1 });
    if (!rest.found || rest.msg_count === 0) return 0;
    left = rest.msg_count;
    // The next agent is the latest writer of what is left; the same one again means nothing moved.
    if (!rest.agent || rest.agent === agent) return left;
    agent = rest.agent;
  }
  return left;
}
