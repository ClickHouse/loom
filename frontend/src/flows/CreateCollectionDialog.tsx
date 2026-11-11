import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import { createCollection, type CollectionEntry } from "../api";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, fieldError } from "../components/ui";
import { distinctTitle, placeholderName, plainName, repeatedTitles } from "../lib/collections";
import { ApiError } from "../lib/errors";
import { humanizeError } from "../lib/errorWords";
import { COLLECTION_PREFIX, isPersonalCollection, NAMESPACE_NAME, PERSONAL_COLLECTION_PREFIX } from "../lib/namespace";
import { invalidateSharedCollections } from "../lib/collectionPlaces";

// The Select's value for "no parent": a Select item cannot carry "".
const TOP_LEVEL = "__top__";
// server/identity/api/models_collections.py: the title field's max_length.
const MAX_TITLE = 200;

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

/** A refused name: what to say, and a free name to offer. */
function nameTaken(e: unknown): { text: string; suggestion: string } | null {
  if (!(e instanceof ApiError)) return null;
  if (e.code === "COLLECTION_NAME_TAKEN") {
    const s = e.detail["suggestion"];
    return { text: humanizeError(e), suggestion: typeof s === "string" ? s : "" };
  }
  return null;
}

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  org: string;
  /** The collections a new one may sit in: those the caller writes, never a personal one. */
  parents: CollectionEntry[];
  /** The parent the form starts on; "" for top level. */
  initialParent?: string;
  /** The header's namespace, named so a top-level collection is not taken to sit in it. */
  headerNamespace?: string | null;
  /** Fires with the collection's name: the new one, or the one the title already names there. */
  onCreated: (namespace: string) => void;
}

/**
 * Create a collection (`collection.create`): a namespace only grants open. A person types
 * its title; the server derives its name (its address), adding a short suffix when the
 * plain one is taken, and a title already used inside the same parent (at the top level,
 * one of the person's own) opens that collection instead. "Choose the address myself"
 * names it explicitly. The creator becomes its admin; under a parent it also inherits the
 * parent's grants, which needs writer on the parent.
 */
export function CreateCollectionDialog({
  open,
  onOpenChange,
  org,
  parents,
  initialParent = "",
  headerNamespace = null,
  onCreated,
}: Props) {
  const [title, setTitle] = useState("");
  const [ownAddress, setOwnAddress] = useState(false);
  const [name, setName] = useState(COLLECTION_PREFIX);
  const [parent, setParent] = useState(initialParent || TOP_LEVEL);
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; suggestion: string } | null>(null);

  const reset = () => {
    setTitle("");
    setOwnAddress(false);
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

  const trimmedTitle = title.trim();
  const trimmed = name.trim();
  // Counted by character after the NFC the server applies (an emoji, or an accent typed
  // as two, is one).
  const titleTooLong = Array.from(trimmedTitle.normalize("NFC")).length > MAX_TITLE;
  const descriptionTooLong = Array.from(description.trim()).length > 2000;
  // Said once something past the prefix is typed, not on the prefilled prefix.
  const nameError = !ownAddress || trimmed === COLLECTION_PREFIX ? null : collectionNameError(trimmed);
  const named = ownAddress ? collectionNameError(trimmed) === null : trimmedTitle !== "";
  const canSubmit = named && !titleTooLong && !descriptionTooLong && !busy;
  const derived = plainName(trimmedTitle);
  const repeated = repeatedTitles(parents);

  const onSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const into = parent === TOP_LEVEL ? "" : parent;
    try {
      const r = ownAddress
        ? await createCollection(org, trimmed, { parent: into, description: description.trim(), title: trimmedTitle })
        : // The plain name rides along for a server a release behind, which creates it.
          await createCollection(org, derived || placeholderName(trimmedTitle), {
            parent: into,
            description: description.trim(),
            title: trimmedTitle,
            deriveName: true,
          });
      invalidateSharedCollections();
      const shown = r.title || trimmedTitle || r.namespace;
      toast(r.created ? `Created ${shown}` : `${shown} already exists here`, r.created ? "ok" : "info");
      onCreated(r.namespace);
      reset();
      onOpenChange(false);
    } catch (e) {
      setError(nameTaken(e) ?? { text: humanizeError(e), suggestion: "" });
      setBusy(false);
    }
  };

  const useSuggestion = (s: string) => {
    setOwnAddress(true);
    setName(s);
    setError(null);
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
        <div>
          <TextField
            id="create-collection-title"
            label="Title"
            value={title}
            onChange={setTitle}
            placeholder="Q3 plans"
            autoFocus
            {...fieldError("create-collection-title-error", titleTooLong ? `At most ${MAX_TITLE} characters.` : null)}
          />
          {!ownAddress && (
            <p className="mt-1 text-xs text-text-muted">
              {derived
                ? `Its address is ${derived}, with a short suffix if that is taken. Agents and grants use the address; people see the title.`
                : "Its address is derived from the title. Agents and grants use the address; people see the title."}
            </p>
          )}
        </div>
        <Checkbox
          label="Choose the address myself"
          checked={ownAddress}
          onCheckedChange={(c) => setOwnAddress(c === true)}
        />
        {ownAddress && (
          <TextField
            id="create-collection-name"
            label="Address"
            value={name}
            onChange={setName}
            placeholder="col-handbook"
            {...fieldError("create-collection-name-error", nameError)}
          />
        )}
        <div>
          <Select
            id="create-collection-parent"
            label="Inside"
            value={parent}
            onSelect={setParent}
            triggerProps={{ "aria-describedby": "create-collection-parent-hint" }}
          >
            <Select.Item value={TOP_LEVEL}>Top level, in the org</Select.Item>
            {parents.map((p) => (
              <Select.Item key={p.namespace} value={p.namespace}>
                {distinctTitle(p, repeated)}
              </Select.Item>
            ))}
          </Select>
          <p id="create-collection-parent-hint" className="mt-1 text-xs text-text-muted">
            {parent === TOP_LEVEL
              ? `A top-level collection belongs to the org${headerNamespace ? `, not to ${headerNamespace}` : ""}: only the members you add read it.`
              : "A collection inside another inherits its grants."}{" "}
            Only collections you can write are listed.
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
        {error && (
          <div className="space-y-2">
            <Alert role="alert" state="danger" text={error.text} />
            {error.suggestion && (
              <Button type="secondary" htmlType="button" onClick={() => useSuggestion(error.suggestion)}>
                {`Use ${error.suggestion}`}
              </Button>
            )}
          </div>
        )}
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
