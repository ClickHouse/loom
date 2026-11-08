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
    const r = await deleteEveryAgent(identity, thread);
    if (r.error) {
      const why = humanizeError(r.error);
      const rest = r.left ? `; ${r.left} messages are left` : "";
      toast(r.parts ? `Deleted part of "${name}", then failed: ${why}${rest}` : `Delete failed: ${why}`, "error");
    } else if (r.left) toast(`Deleted part of "${name}"; ${r.left} messages are left`, "info");
    else toast(`Deleted "${name}"`, "ok");
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
      // The row belongs to the agent that opened the thread, not always the last writer;
      // a thread built by appends has none yet, so one is written with the title.
      const newTitle = title.trim();
      const head = await getConversationThread(identity, "*", thread.thread_id, { limit: 1 });
      if (!head.found) throw new Error("This conversation was deleted");
      // A row deleted since the read answers `updated: false` too.
      const { updated } = head.created_at
        ? await renameThread(identity, head.agent, thread.thread_id, newTitle)
        : { updated: false };
      if (!updated) await createThreadRow(identity, thread.agent, thread.thread_id, newTitle);
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

/** Delete the thread one agent at a time, the list's agent first, until nothing of it is
 *  left: no turn and no row. Answers how many parts it deleted, the messages left when it
 *  stopped early, and the error that stopped it. */
async function deleteEveryAgent(
  id: Pick<Identity, "org" | "namespace">,
  thread: Thread,
): Promise<{ parts: number; left: number; error?: unknown }> {
  let agent = thread.agent;
  let parts = 0;
  let left = 0;
  try {
    while (parts < MAX_AGENTS) {
      await deleteThread(id, agent, thread.thread_id);
      parts++;
      // What was left before this delete is no longer known.
      left = 0;
      const rest = await getConversationThread(id, "*", thread.thread_id, { limit: 1 });
      if (!rest.found) return { parts, left: 0 };
      left = rest.msg_count;
      // Next: the row's owner while a row is left, else the oldest remaining turn's writer.
      // The same agent again means its delete took nothing more.
      if (!rest.agent || rest.agent === agent) break;
      agent = rest.agent;
    }
  } catch (error) {
    return { parts, left, error };
  }
  return { parts, left };
}
