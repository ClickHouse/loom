import { useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X, Settings as SettingsIcon, Trash2 } from "lucide-react";
import {
  getNamespace,
  reregisterNamespace,
  deleteNamespace,
  type NamespaceInfo,
  type NamespaceRegisterFields,
} from "../api";
import { useStore } from "../lib/state";
import { canUpdateNamespace, isNamespaceAdmin } from "../lib/role";
import { toast } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { humanizeError } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  namespace: string;
}

/**
 * Namespace-level settings: description, visibility, approval gate,
 * delete. SEPARATE from MembersDialog so admins don't accidentally
 * flip namespace-wide knobs while doing per-person invitations.
 *
 * Members dialog = "people in this namespace + their roles"
 * Settings dialog = "what is this namespace"
 *
 * Both open from Discover's "Namespaces you manage" section.
 */
export function NamespaceSettingsDialog({ open, onOpenChange, org, namespace }: Props) {
  const whoami = useStore((s) => s.whoami);
  const myNamespaces = useStore((s) => s.myNamespaces);
  // A loaded row is kept with the (org, namespace) it was read for, so a row read for a
  // previous namespace is never shown under this one or used to build its save.
  const key = JSON.stringify([org, namespace]);
  const keyRef = useRef(key);
  keyRef.current = key;
  const [loaded, setLoaded] = useState<{ key: string; info: NamespaceInfo | null } | null>(null);
  const ns = loaded?.key === key ? loaded.info : null;
  const [descDraft, setDescDraft] = useState("");
  const [descDirty, setDescDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  // The cost-cap field's text while it is being edited, kept with the namespace it was typed
  // for; null (or another namespace's draft) shows the stored cap.
  const [capEdit, setCapEdit] = useState<{ key: string; text: string } | null>(null);
  const capDraft = capEdit?.key === key ? capEdit.text : null;

  useAbortableEffect(
    async (signal, live) => {
      // A draft never outlives the open session it was typed in: a close that delivers no
      // blur (overlay click, parent-driven close) must not bring it back on reopen.
      setCapEdit(null);
      if (!open) return;
      const info = await getNamespace({ org, namespace }, { signal }).catch(() => null);
      if (!live()) return;
      setLoaded({ key, info });
      setDescDraft(info?.description || "");
      setDescDirty(false);
    },
    [open, org, namespace],
  );

  // The settings round-trip namespace.register, whose update path also admits the owner of an
  // auto-registered row (and on a restricted row only a named, directly granted admin);
  // namespace.delete needs ADMIN on the namespace.
  const canManage = canUpdateNamespace(whoami, myNamespaces, namespace, ns);
  const canDelete = isNamespaceAdmin(whoami, myNamespaces, namespace);

  // Repository mode: a code index shows only what applies to read-only source —
  // description, the join rule, danger zone.
  const isRepo = ns?.kind === "code";

  const save = async (patch: Partial<NamespaceRegisterFields>) => {
    if (!ns) return;
    const savedKey = key;
    setSaving(true);
    try {
      await reregisterNamespace({ org, namespace }, ns, patch);
      toast("Settings saved", "ok");
      const next = await getNamespace({ org, namespace });
      // The dialog moved to another namespace while this save was in flight.
      if (keyRef.current !== savedKey) return;
      setLoaded({ key: savedKey, info: next });
      if (patch.description !== undefined) {
        setDescDraft(next?.description || "");
        setDescDirty(false);
      }
    } catch (e) {
      toast(`Save failed: ${humanizeError(e)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  // Blur (and Enter, which blurs) commits a finite non-negative cap that differs from the
  // stored one; anything else — empty, partial, negative — reverts without a request.
  const commitCap = async () => {
    if (capDraft === null || !ns) return;
    const v = Number(capDraft);
    if (capDraft.trim() !== "" && Number.isFinite(v) && v >= 0 && v !== (ns.daily_cost_cap_usd ?? 0)) {
      await save({ daily_cost_cap_usd: v });
    }
    setCapEdit(null);
  };

  const onDelete = async () => {
    if (
      !(await confirmAction({
        title: `Delete namespace "${namespace}"?`,
        message:
          "The namespace is deactivated. Existing memories and grants stay queryable for forensics. To permanently erase data, run an erasure.request separately.",
        confirmLabel: "Delete namespace",
        danger: true,
      }))
    )
      return;
    setSaving(true);
    try {
      const r = await deleteNamespace({ org, namespace });
      if (r.deleted) {
        toast(`Deleted ${namespace}`, "ok");
        onOpenChange(false);
        setTimeout(() => location.reload(), 600);
      } else {
        toast(`Namespace already inactive or not found`, "info");
      }
    } catch (e) {
      toast(`Delete failed: ${humanizeError(e)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content
          onEscapeKeyDown={(e) => {
            // Escape while editing the cap restores it; the next Escape closes the dialog.
            if (capDraft === null) return;
            e.preventDefault();
            setCapEdit(null);
          }}
          className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(600px,92vw)] max-h-[85vh] flex flex-col rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!"
        >
          <div className="flex items-center justify-between gap-3 px-6 py-4 border-b border-[var(--color-border)] shrink-0">
            <Dialog.Title className="text-lg font-semibold tracking-tight flex items-center gap-2 min-w-0">
              <SettingsIcon size={16} className="shrink-0" />
              <span className="truncate">
                Settings · <span className="font-mono">{namespace}</span>
              </span>
            </Dialog.Title>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              aria-label="Close"
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          <div className="px-6 py-5 flex-1 overflow-y-auto">
            {!ns && <div className="text-sm text-[var(--color-text-muted)] text-center py-8">Loading…</div>}
            {ns && (
              <div className="space-y-5">
                {/* Description */}
                <div>
                  <label
                    htmlFor="ns-settings-description"
                    className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
                  >
                    Description
                  </label>
                  <div className="flex gap-2">
                    <input
                      id="ns-settings-description"
                      value={descDraft}
                      disabled={!canManage || saving}
                      onChange={(e) => {
                        setDescDraft(e.target.value);
                        setDescDirty(e.target.value !== (ns.description || ""));
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && descDirty) save({ description: descDraft });
                      }}
                      placeholder="What is this namespace for?"
                      className="flex-1 px-3 py-1.5 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)] disabled:opacity-60"
                    />
                    {canManage && descDirty && (
                      <button
                        onClick={() => save({ description: descDraft })}
                        disabled={saving}
                        className="px-3 py-1.5 text-xs font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-50"
                      >
                        Save
                      </button>
                    )}
                  </div>
                </div>

                {!isRepo && (
                  <>
                    {/* Visibility */}
                    <div>
                      <label className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5">
                        Visibility
                      </label>
                      <div className="inline-flex rounded-md border border-[var(--color-border)] overflow-hidden">
                        {(["private", "public"] as const).map((v) => (
                          <button
                            key={v}
                            onClick={() => save({ visibility: v })}
                            disabled={!canManage || saving || (ns.visibility || "private") === v}
                            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                              (ns.visibility || "private") === v
                                ? "bg-[var(--color-accent)] text-[var(--color-on-accent)]"
                                : "bg-[var(--color-surface)] text-[var(--color-text)] hover:bg-[var(--color-surface-2)] disabled:opacity-50"
                            }`}
                          >
                            {v === "private" ? "Private" : "Public"}
                          </button>
                        ))}
                      </div>
                      <div className="text-2xs text-[var(--color-text-muted)] mt-1.5">
                        {(ns.visibility || "private") === "private"
                          ? "Only members you grant. Doesn't appear in Spaces."
                          : "Anyone in the org can find + join this namespace via Spaces."}
                      </div>
                    </div>

                    {/* Daily cost ceiling */}
                    <div>
                      <label
                        htmlFor="ns-cost-cap"
                        className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
                      >
                        Daily LLM budget (USD)
                      </label>
                      <div className="flex items-center gap-2">
                        <input
                          id="ns-cost-cap"
                          type="number"
                          step="0.01"
                          min="0"
                          value={capDraft ?? String(ns.daily_cost_cap_usd ?? 0)}
                          disabled={!canManage || saving}
                          onChange={(e) => setCapEdit({ key, text: e.target.value })}
                          onBlur={() => void commitCap()}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") e.currentTarget.blur();
                          }}
                          className="w-32 px-3 py-1.5 text-sm tabular bg-[var(--color-bg)] border border-[var(--color-border)] rounded-md disabled:opacity-50"
                        />
                        <span className="text-xs text-[var(--color-text-muted)]">
                          {(ns.daily_cost_cap_usd ?? 0) === 0
                            ? "inherits the global default"
                            : "per 24h rolling window"}
                        </span>
                      </div>
                      <div className="text-2xs text-[var(--color-text-muted)] mt-1.5 leading-relaxed">
                        Once this namespace's upstream LLM spend over the last 24h reaches the cap, background entity
                        extraction, code summaries and code-ingest descriptions skip, code.search skips query expansion,
                        and memory.ask, code.ask and knowledge.synthesize answer without synthesis until spend drops.
                        Writes are still accepted. 0 inherits the global default.
                      </div>
                    </div>
                  </>
                )}

                {/* Join policy */}
                <div>
                  <label className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5">
                    How people join
                  </label>
                  <div className="flex gap-1 flex-wrap">
                    {(isRepo
                      ? ([
                          ["open", "Open", "Anyone in the org joins instantly as a reader."],
                          ["approval", "Restricted", "A maintainer approves each reader."],
                          ["closed", "Closed", "Nobody can request; maintainers add readers by name."],
                        ] as const)
                      : ([
                          [
                            "inherit",
                            "Inherit",
                            "Follow visibility: public rows join instantly as viewer, private rows ask an admin.",
                          ],
                          ["open", "Open", "Anyone in the org joins instantly as viewer; writer and admin still ask."],
                          ["approval", "Approval", "Every join waits for an admin's approval."],
                          ["closed", "Closed", "Nobody self-joins; admins add members directly."],
                        ] as const)
                    ).map(([v, label, hint]) => (
                      <button
                        key={v}
                        type="button"
                        title={hint}
                        onClick={() => save({ join_policy: v })}
                        disabled={!canManage || saving || (ns.join_policy || "inherit") === v}
                        className={`px-3 py-1.5 text-sm rounded-md border ${
                          (ns.join_policy || "inherit") === v
                            ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-text)]"
                            : "border-[var(--color-border)] text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)]"
                        } disabled:opacity-60`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <div className="text-2xs text-[var(--color-text-muted)] mt-1.5">
                    {isRepo ? (
                      <>
                        Right now this repository is{" "}
                        <span className="font-medium text-[var(--color-text)]">
                          {ns.effective_join_policy === "approval"
                            ? "restricted"
                            : ns.effective_join_policy || "restricted"}
                        </span>
                        . Every repository is listed and searchable once you read it; only the way in differs. Readers
                        never write; maintainers are named by an org admin.
                      </>
                    ) : (
                      <>
                        Right now this namespace is{" "}
                        <span className="font-medium text-[var(--color-text)]">
                          {ns.effective_join_policy || "approval"}
                        </span>
                        .
                      </>
                    )}
                  </div>
                </div>

                {!isRepo && (
                  <>
                    {/* Approval gate */}
                    <label className="flex items-start gap-2.5 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={!!ns.requires_approval}
                        disabled={!canManage || saving}
                        onChange={(e) => save({ requires_approval: e.target.checked })}
                        className="mt-0.5"
                      />
                      <div>
                        <div className="text-sm">Require approval for viewer (read-only) joins too</div>
                        <div className="text-2xs text-[var(--color-text-muted)] mt-0.5">
                          Writer and admin joins always require approval. Turn this on if even read-only access should
                          be reviewed first.
                        </div>
                      </div>
                    </label>

                    {/* Prompt capture consent — opt IN. Consent is given, not
                    withdrawn: a query is something a person typed, so capturing by
                    default would collect everyone's until they found this and
                    objected. Lives with the namespace because that is the
                    per-person unit here. Written explicitly true/false rather than
                    true/null so the namespace's answer always wins and an operator
                    flipping the server default cannot silently re-enable someone. */}
                    <label className="flex items-start gap-2.5 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={ns.capture_generation_io === true}
                        disabled={!canManage || saving}
                        onChange={(e) => save({ capture_generation_io: e.target.checked })}
                        className="mt-0.5"
                      />
                      <div>
                        <div className="text-sm">Record what I search and what I store, for debugging</div>
                        <div className="text-2xs text-[var(--color-text-muted)] mt-0.5">
                          Off unless you tick it, so none of your text is kept. Tick this and loom records your{" "}
                          <em>search wording</em> plus the memory text and conversation turns it sends to the model
                          while answering &mdash; which is what makes a bad recall diagnosable afterwards. Counts, cost,
                          latency, recall and quality scores are kept either way; those carry no text. Applies to this
                          namespace only; takes effect within a minute.
                        </div>
                      </div>
                    </label>
                  </>
                )}

                {/* Danger zone */}
                {canDelete && (
                  <div className="pt-4 mt-2 border-t border-[var(--color-border)]">
                    <div className="text-2xs uppercase tracking-wider text-danger font-semibold mb-2">Danger zone</div>
                    <div className="flex items-start justify-between gap-3">
                      <div className="text-2xs text-[var(--color-text-muted)]">
                        Delete deactivates the namespace. Existing data stays queryable for forensics. For full erasure
                        run an erasure.request separately.
                      </div>
                      <button
                        onClick={onDelete}
                        disabled={saving}
                        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-danger/40 text-danger hover:bg-danger/15 rounded-md disabled:opacity-50 shrink-0"
                      >
                        <Trash2 size={12} /> Delete namespace
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
