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
          <p className="text-sm text-text-muted">Everyone who reaches the new parent reaches this collection too.</p>
          <Select id="move-collection-parent" label="Inside" value={parent} onSelect={setParent} showSearch>
            <Select.Item value={TOP}>Top level</Select.Item>
            {parents
              .filter((c) => c.namespace !== collection.namespace)
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
