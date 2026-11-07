import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { moveCollection, type CollectionEntry } from "../api";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert } from "../components/ui";

// A Select value for "no parent": Click UI's Select takes no empty value.
const TOP = "__top__";

/** Move a collection under another (collection.move), or to the top level. The server
 *  refuses a move into the collection's own subtree. */
export function MoveCollectionDialog({
  org,
  collection,
  parents,
  onMoved,
  onClose,
}: {
  org: string;
  collection: CollectionEntry;
  /** The collections it may sit in: those the caller writes. */
  parents: CollectionEntry[];
  onMoved: () => void;
  onClose: () => void;
}) {
  const [parent, setParent] = useState(collection.parent || TOP);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const target = parent === TOP ? "" : parent;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await moveCollection(org, collection.namespace, target);
      toast(target ? `Moved ${collection.namespace} into ${target}` : `${collection.namespace} is top-level`, "ok");
      onMoved();
      onClose();
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onOpenChange={(v) => !v && onClose()}
      title={`Move ${collection.namespace}`}
      className="w-full max-w-[480px]"
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-4">
          <p className="text-sm text-text-muted">{moveEffect(collection.parent, target)}</p>
          <Select id="move-collection-parent" label="Inside" value={parent} onSelect={setParent} showSearch>
            <Select.Item value={TOP}>Top level</Select.Item>
            {parents
              .filter((c) => !inside(c, collection.namespace, parents))
              .map((c) => (
                <Select.Item key={c.namespace} value={c.namespace}>
                  {c.namespace}
                </Select.Item>
              ))}
          </Select>
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={busy || target === collection.parent} loading={busy}>
            Move
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}

/** Whether `c` is `root` or sits under it: a collection cannot move into its own subtree. */
function inside(c: CollectionEntry, root: string, all: CollectionEntry[]): boolean {
  const byName = new Map(all.map((e) => [e.namespace, e]));
  for (let at: CollectionEntry | undefined = c; at; at = at.parent ? byName.get(at.parent) : undefined)
    if (at.namespace === root) return true;
  return false;
}

/** Who reaches the collection after the move: its own members always, the new parent's
 *  readers when it has one; the old parent's readers lose the access they had through it. */
function moveEffect(from: string, to: string): string {
  const lose = from && from !== to ? ` Whoever reached it only through ${from} loses it.` : "";
  const gain = to ? `Everyone who reaches ${to} reaches it too.` : "At the top, only its own members reach it.";
  return gain + lose;
}
