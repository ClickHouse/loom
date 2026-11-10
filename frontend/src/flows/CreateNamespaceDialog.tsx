import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { TextField } from "@clickhouse/click-ui/TextField";
import { registerNamespace, setSavedNamespace } from "../api";
import { useStore } from "../lib/state";
import { toast } from "../components/Toast";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, fieldError } from "../components/ui";
import { humanizeError } from "../lib/errorWords";
import { NAMESPACE_NAME } from "../lib/namespace";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated?: (namespace: string) => void;
}

/**
 * Create-namespace dialog wired into the namespace picker.
 *
 * Anyone authenticated can create a namespace (`namespace.register` is
 * VIEWER-gated and the caller auto-becomes admin). Visibility defaults
 * to private — public is the deliberate choice for team scratchpads.
 *
 * After creation the SPA switches identity to the new namespace so the
 * user lands in their fresh workspace.
 */
export function CreateNamespaceDialog({ open, onOpenChange, onCreated }: Props) {
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const setIdentity = useStore((s) => s.setIdentity);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<"private" | "public">("private");
  // Only meaningful when visibility=public. When true, viewer self-
  // joins ALSO go pending (writer/admin already always do). Used for
  // sensitive public namespaces where even read-access needs admin
  // sign-off.
  const [requiresApproval, setRequiresApproval] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setDescription("");
    setVisibility("private");
    setRequiresApproval(false);
    setBusy(false);
    setError(null);
  };

  // Namespaces are lowercase + kebab-case by convention; reject obviously
  // malformed input before round-tripping to the server.
  const trimmed = name.trim();
  const nameValid = NAMESPACE_NAME.test(trimmed);
  const canSubmit = nameValid && !busy;

  const onSubmit = async () => {
    if (!canSubmit || !identity) return;
    setBusy(true);
    setError(null);
    try {
      await registerNamespace(
        { org: identity.org, namespace: trimmed },
        {
          owner_email: whoami?.subject || "",
          description: description.trim(),
          visibility,
          requires_approval: visibility === "public" ? requiresApproval : false,
        },
      );
      toast(`Created ${trimmed}`, "ok");
      // Switch to the freshly-created namespace.
      setIdentity({ ...identity, namespace: trimmed });
      setSavedNamespace(trimmed);
      onCreated?.(trimmed);
      reset();
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  const close = (v: boolean) => {
    if (!v) reset();
    onOpenChange(v);
  };

  return (
    <Modal open={open} onOpenChange={close} title="Create namespace" className="w-full max-w-[520px]">
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          void onSubmit();
        }}
      >
        <TextField
          id="create-ns-name"
          label="Name (lowercase, kebab-case)"
          value={name}
          onChange={setName}
          placeholder="my-team"
          autoFocus
          {...fieldError(
            "create-ns-name-error",
            trimmed && !nameValid && "Use lowercase letters, digits, and dashes only. Start with a letter or digit.",
          )}
        />
        <TextField
          id="create-ns-description"
          label="Description (optional)"
          value={description}
          onChange={setDescription}
          placeholder="e.g. ClickHouse kernel team shared memory"
        />
        <ChoiceGroup
          label="Visibility"
          value={visibility}
          onChange={setVisibility}
          options={[
            { value: "private", label: "Private", hint: "Only you and people you grant. Default." },
            { value: "public", label: "Public", hint: "Anyone in the org can browse + join. For team scratchpads." },
          ]}
        />
        {visibility === "public" && (
          <div>
            <Checkbox
              label="Require approval for read-only (viewer) joins too"
              aria-describedby="create-ns-approval-hint"
              checked={requiresApproval}
              onCheckedChange={(x) => setRequiresApproval(x === true)}
            />
            <p id="create-ns-approval-hint" className="mt-1 text-xs text-text-muted">
              Writer and admin joins always need approval. Turn this on if even viewers should be reviewed first.
            </p>
          </div>
        )}
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
