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
          onClose={(changed) => {
            setRenaming(false);
            if (changed) onChanged();
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
  onClose: (changed: boolean) => void;
}) {
  const [title, setTitle] = useState(thread.title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    try {
      const renamed = await retitle(identity, thread, title.trim());
      toast(renamed ? "Renamed" : "This conversation was deleted", renamed ? "ok" : "info");
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

/** The thread's first turn across every agent; `agent` is the row's owner while a row is
 *  left, else the oldest remaining turn's writer. */
const readHead = (id: Pick<Identity, "org" | "namespace">, threadId: string) =>
  getConversationThread(id, "*", threadId, { limit: 1 });

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
      const rest = await readHead(id, thread.thread_id);
      if (!rest.found) return { parts, left: 0 };
      left = rest.msg_count;
      // The same agent again means its delete took nothing more.
      if (!rest.agent || rest.agent === agent) break;
      agent = rest.agent;
    }
  } catch (error) {
    return { parts, left, error };
  }
  return { parts, left };
}

/** Title the thread: update its row as the agent that owns it (not always the last
 *  writer), or write one for a thread built by appends, which has none. Answers false
 *  when the conversation is gone. */
async function retitle(id: Pick<Identity, "org" | "namespace">, thread: Thread, title: string): Promise<boolean> {
  const head = await readHead(id, thread.thread_id);
  if (!head.found) return false;
  if (head.created_at) {
    if ((await renameThread(id, head.agent, thread.thread_id, title)).updated) return true;
    // The row went since the read: write one only while the conversation still has turns.
    if (!(await readHead(id, thread.thread_id)).found) return false;
  }
  await createThreadRow(id, thread.agent, thread.thread_id, title);
  return true;
}
