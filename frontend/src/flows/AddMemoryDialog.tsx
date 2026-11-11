import { useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { setMemory, type Identity } from "../api";
import { sharedTagVocabulary } from "../lib/sharedReads";
import { humanizeError, toastCommitted } from "../lib/errorWords";
import { isPublicationPending } from "../lib/errors";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, fieldError } from "../components/ui";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  identity: Identity;
  onAdded: () => void;
}

// "Never" leaves ttl_seconds unset → no `expires_at`; lives until deleted.
const EXPIRY_OPTIONS: Array<{ label: string; ttlSeconds: number | null }> = [
  { label: "Never", ttlSeconds: null },
  { label: "1 hour", ttlSeconds: 60 * 60 },
  { label: "1 day", ttlSeconds: 24 * 60 * 60 },
  { label: "1 week", ttlSeconds: 7 * 24 * 60 * 60 },
  { label: "30 days", ttlSeconds: 30 * 24 * 60 * 60 },
  { label: "90 days", ttlSeconds: 90 * 24 * 60 * 60 },
  { label: "1 year", ttlSeconds: 365 * 24 * 60 * 60 },
];

export function AddMemoryDialog({ open, onOpenChange, identity, onAdded }: Props) {
  const [memoryKey, setMemoryKey] = useState("");
  const [value, setValue] = useState("");
  const [tagsInput, setTagsInput] = useState("");
  const [expiryIdx, setExpiryIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Namespace tags, surfaced as datalist suggestions on the tag input.
  const [existingTags, setExistingTags] = useState<string[]>([]);
  useEffect(() => {
    if (!open) return;
    sharedTagVocabulary(identity)
      .then(setExistingTags)
      .catch(() => setExistingTags([]));
  }, [open, identity.org, identity.namespace]);

  const reset = () => {
    setMemoryKey("");
    setValue("");
    setTagsInput("");
    setExpiryIdx(0);
    setError(null);
    setBusy(false);
  };

  const close = (v: boolean) => {
    if (!v) reset();
    onOpenChange(v);
  };

  const keyError =
    memoryKey.trim() === ""
      ? null
      : !/^[a-z][a-z0-9_]{0,79}$/.test(memoryKey.trim())
        ? "Must be lower_snake_case, start with a letter, ≤80 chars"
        : null;

  const canSubmit = memoryKey.trim() !== "" && value.trim() !== "" && !keyError && !busy;

  const onSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const tags = tagsInput
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t.length > 0);
    try {
      // Attribute to `loom-ui`, not the picker (which is "view as", not the
      // writer) — Conflicts/Audit/Spending key off `agent`.
      await setMemory({ ...identity, agent: "loom-ui" }, memoryKey.trim(), value.trim(), {
        memoryType: "semantic",
        tags,
        ttlSeconds: EXPIRY_OPTIONS[expiryIdx].ttlSeconds,
      });
      toast("Memory added", "ok");
    } catch (e) {
      // Added, with only lists and search behind: the dialog closes as after a success, so
      // it is not sent again.
      if (!isPublicationPending(e)) {
        setError(humanizeError(e));
        setBusy(false);
        return;
      }
      toastCommitted(e);
    }
    onAdded();
    reset();
    onOpenChange(false);
  };

  return (
    <Modal open={open} onOpenChange={close} title="Add memory" className="w-full max-w-[560px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void onSubmit();
        }}
      >
        <div className="space-y-4">
          <TextField
            id="add-memory-key"
            label="Key (lower_snake_case)"
            value={memoryKey}
            onChange={setMemoryKey}
            placeholder="user_coffee_preference"
            {...fieldError("add-memory-key-error", keyError)}
            autoFocus
          />
          <TextAreaField
            id="add-memory-value"
            label="Value"
            value={value}
            onChange={setValue}
            placeholder="User takes coffee black, no sugar."
            rows={4}
          />
          <TextField
            id="add-memory-tags"
            label="Tags (comma-separated, optional)"
            value={tagsInput}
            onChange={setTagsInput}
            placeholder="preference, beverage"
            list="loom-existing-tags"
          />
          {existingTags.length > 0 && (
            <datalist id="loom-existing-tags">
              {existingTags.map((t) => (
                <option key={t} value={t} />
              ))}
            </datalist>
          )}
          <Select
            id="add-memory-expiry"
            label="Expires (auto-delete after)"
            value={String(expiryIdx)}
            onSelect={(v) => setExpiryIdx(Number.parseInt(v, 10))}
          >
            {EXPIRY_OPTIONS.map((opt, i) => (
              <Select.Item key={opt.label} value={String(i)}>
                {opt.label}
              </Select.Item>
            ))}
          </Select>
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={!canSubmit} loading={busy}>
            Add memory
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
