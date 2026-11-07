import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import { deleteThread, renameThread, type Identity } from "../api";
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
  tabIndex,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  thread: Thread;
  canRename: boolean;
  canDelete: boolean;
  onChanged: () => void;
  tabIndex?: number;
}) {
  const [renaming, setRenaming] = useState(false);
  const name = thread.title || thread.thread_id;

  const remove = async () => {
    const ok = await confirmAction({
      title: `Delete "${name}"?`,
      message: "Its messages leave search and the replay; they can be restored for 14 days.",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await deleteThread(identity, thread.agent, thread.thread_id);
      toast(`Deleted "${name}"`, "ok");
      onChanged();
    } catch (e) {
      toast(`Delete failed: ${humanizeError(e)}`, "error");
    }
  };

  const items: RowMenuItem[] = [
    ...(canRename ? [{ label: "Rename", icon: "pencil" as const, onSelect: () => setRenaming(true) }] : []),
    ...(canDelete ? [{ label: "Delete", icon: "trash" as const, danger: true, onSelect: () => void remove() }] : []),
  ];
  if (items.length === 0) return null;
  return (
    <>
      <RowMenu name={name} items={items} tabIndex={tabIndex} />
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
      await renameThread(identity, thread.agent, thread.thread_id, title.trim());
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
