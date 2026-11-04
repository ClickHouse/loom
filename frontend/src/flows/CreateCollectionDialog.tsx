import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import { createCollection, type CollectionEntry } from "../api";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, fieldError } from "../components/ui";
import { humanizeError } from "../lib/format";
import { isPersonalCollection, NAMESPACE_NAME, PERSONAL_COLLECTION_PREFIX } from "../lib/namespace";

// server/auth/sharing.py: a collection's name starts with `col-`, and `col-me-` is kept
// for the personal collections the server names.
const COLLECTION_PREFIX = "col-";
// The Select's value for "no parent": a Select item cannot carry "".
const TOP_LEVEL = "__top__";

/** Why `name` cannot be a collection's name, or null when it can. */
export function collectionNameError(name: string): string | null {
  if (!name.startsWith(COLLECTION_PREFIX) || name.length === COLLECTION_PREFIX.length) {
    return `Start the name with "${COLLECTION_PREFIX}", then lowercase letters, digits and dashes.`;
  }
  if (isPersonalCollection(name)) return `"${PERSONAL_COLLECTION_PREFIX}" is kept for personal collections.`;
  if (!NAMESPACE_NAME.test(name)) return "Use lowercase letters, digits and dashes only, at most 128 characters.";
  return null;
}

/** The collections a new one may sit in: those the caller writes, never a personal one,
 *  which holds no other collection. */
export function parentChoices(collections: CollectionEntry[]): CollectionEntry[] {
  return collections.filter((c) => !c.personal && c.role !== "viewer");
}

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  org: string;
  /** The collections a new one may sit in: those the caller writes, never a personal one. */
  parents: CollectionEntry[];
  /** The parent the form starts on; "" for top level. */
  initialParent?: string;
  /** Fires with the new collection's name. */
  onCreated: (namespace: string) => void;
}

/**
 * Create a collection (`collection.create`): a namespace only grants open. The creator
 * becomes its admin; under a parent it also inherits the parent's grants, which needs
 * writer on the parent.
 */
export function CreateCollectionDialog({ open, onOpenChange, org, parents, initialParent = "", onCreated }: Props) {
  const [name, setName] = useState(COLLECTION_PREFIX);
  const [parent, setParent] = useState(initialParent || TOP_LEVEL);
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName(COLLECTION_PREFIX);
    setParent(initialParent || TOP_LEVEL);
    setDescription("");
    setBusy(false);
    setError(null);
  };
  const close = (v: boolean) => {
    if (!v) reset();
    onOpenChange(v);
  };

  const trimmed = name.trim();
  // Said once something past the prefix is typed, not on the prefilled prefix.
  const nameError = trimmed === COLLECTION_PREFIX ? null : collectionNameError(trimmed);
  // Counted by character, as the server counts it (an emoji is one).
  const descriptionTooLong = Array.from(description.trim()).length > 2000;
  const canSubmit = collectionNameError(trimmed) === null && !descriptionTooLong && !busy;

  const onSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const r = await createCollection(org, trimmed, {
        parent: parent === TOP_LEVEL ? "" : parent,
        description: description.trim(),
      });
      toast(r.created ? `Created ${r.namespace}` : `${r.namespace} already exists`, "ok");
      onCreated(r.namespace);
      reset();
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onOpenChange={close}
      title="New collection"
      description="Only grants open a collection. You become its admin."
      className="w-full max-w-[520px]"
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          void onSubmit();
        }}
      >
        <TextField
          id="create-collection-name"
          label="Name"
          value={name}
          onChange={setName}
          placeholder="col-handbook"
          autoFocus
          {...fieldError("create-collection-name-error", nameError)}
        />
        <div>
          <Select
            id="create-collection-parent"
            label="Inside"
            value={parent}
            onSelect={setParent}
            triggerProps={{ "aria-describedby": "create-collection-parent-hint" }}
          >
            <Select.Item value={TOP_LEVEL}>None (top level)</Select.Item>
            {parents.map((p) => (
              <Select.Item key={p.namespace} value={p.namespace}>
                {p.namespace}
              </Select.Item>
            ))}
          </Select>
          <p id="create-collection-parent-hint" className="mt-1 text-xs text-text-muted">
            A collection inside another inherits its grants. Only collections you can write are listed.
          </p>
        </div>
        <TextField
          id="create-collection-description"
          label="Description (optional)"
          value={description}
          onChange={setDescription}
          placeholder="What the collection holds"
          {...fieldError("create-collection-description-error", descriptionTooLong ? "At most 2000 characters." : null)}
        />
        {error && <Alert role="alert" state="danger" text={error} />}
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" iconLeft="plus" disabled={!canSubmit} loading={busy}>
            Create
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
