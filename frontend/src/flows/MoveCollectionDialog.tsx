import { useMemo, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { moveCollection, type CollectionEntry } from "../api";
import { humanizeError } from "../lib/errorWords";
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
  all,
  onMoved,
  onClose,
}: {
  org: string;
  collection: CollectionEntry;
  /** The collections it may sit in: those the caller writes. */
  parents: CollectionEntry[];
  /** Every collection the person sees, so the tree walks through ones they only read. */
  all: CollectionEntry[];
  onMoved: () => void;
  onClose: () => void;
}) {
  const [parent, setParent] = useState(collection.parent || TOP);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const target = parent === TOP ? "" : parent;
  const parentOf = useMemo(() => new Map(all.map((e) => [e.namespace, e.parent])), [all]);

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
          <p className="text-sm text-text-muted">{moveEffect(collection.parent, target, parentOf)}</p>
          <Select id="move-collection-parent" label="Inside" value={parent} onSelect={setParent} showSearch>
            <Select.Item value={TOP}>Top level</Select.Item>
            {parents
              .filter((c) => !under(c.namespace, collection.namespace, parentOf))
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

/** Whether `name` is `root` or sits somewhere under it. */
function under(name: string, root: string, parentOf: Map<string, string>): boolean {
  const seen = new Set<string>();
  for (let at: string | undefined = name; at && !seen.has(at); at = parentOf.get(at)) {
    if (at === root) return true;
    seen.add(at);
  }
  return false;
}

/** Who reaches the collection after the move: its own members always, the new parent's
 *  readers when it has one. The old parent's readers lose the access they had through it,
 *  unless the new parent sits under the old one. */
function moveEffect(from: string, to: string, parentOf: Map<string, string>): string {
  const keeps = !from || (!!to && under(to, from, parentOf));
  const lose = keeps ? "" : ` Whoever reached it only through ${from} loses it.`;
  const gain = to ? `Everyone who reaches ${to} reaches it too.` : "At the top, only its own members reach it.";
  return gain + lose;
}
