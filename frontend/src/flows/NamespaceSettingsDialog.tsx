import { useEffect, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { NumberField } from "@clickhouse/click-ui/NumberField";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  getNamespace,
  reregisterNamespace,
  deleteNamespace,
  type MyNamespaceEntry,
  type NamespaceInfo,
  type NamespaceRegisterFields,
} from "../api";
import { useStore } from "../lib/state";
import { hasAdminClaim, isNamespaceAdmin } from "../lib/role";
import { canUpdateNamespace, restrictedMisses } from "../lib/namespaceUpdate";
import { needsRole } from "../lib/namespaceRole";
import { sharedMine } from "../lib/sharedReads";
import {
  holdsDirectRow,
  needsMaintainer,
  rolesUnseen,
  scopedUnseen,
  serverDecides,
  unreadLine,
} from "../lib/callReason";
import { toast, toastError } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { useAbortableEffect } from "../lib/hooks";
import { oneOf } from "../lib/oneOf";
import { Modal } from "../components/ui/Modal";
import { Loading, Unavailable } from "../components/ui";

const REPO_JOIN_RULES = [
  ["open", "Open", "Anyone in the org joins instantly as a reader."],
  ["approval", "Restricted", "A maintainer approves each reader."],
  ["closed", "Closed", "Nobody can request; maintainers add readers by name."],
] as const;

const NAMESPACE_JOIN_RULES = [
  ["inherit", "Inherit", "Follow visibility: public rows join instantly as viewer, private rows ask an admin."],
  ["open", "Open", "Anyone in the org joins instantly as viewer; writer and admin still ask."],
  ["approval", "Approval", "Every join waits for an admin's approval."],
  ["closed", "Closed", "Nobody self-joins; admins add members directly."],
] as const;

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  namespace: string;
  /** A repository's name ("owner/name @ branch"), which its reasons name it by. */
  label?: string;
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
export function NamespaceSettingsDialog({ open, onOpenChange, org, namespace, label }: Props) {
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

  const [mineRead, setMineRead] = useState<{ org: string; rows: MyNamespaceEntry[] | null } | null>(null);
  // The caller's grants: App's boot read, else this dialog's own (a restricted row's gate
  // wants the grant even under an admin claim). Null until read, and on a failed read.
  useEffect(() => {
    if (!open || myNamespaces !== null) return;
    let live = true;
    sharedMine(org).then(
      (r) => live && setMineRead({ org, rows: r }),
      () => live && setMineRead({ org, rows: null }),
    );
    return () => {
      live = false;
    };
  }, [open, org, myNamespaces]);
  const mineLoading = myNamespaces === null && mineRead?.org !== org;
  const rows = myNamespaces ?? (mineLoading ? null : (mineRead?.rows ?? null));
  // The settings round-trip namespace.register, whose update path also admits the owner of an
  // auto-registered row (and on a restricted row only a named, directly granted admin);
  // namespace.delete needs ADMIN on the namespace.
  const canManage = canUpdateNamespace(whoami, rows, { org, namespace }, ns);

  // Repository mode: a code index shows only what applies to read-only source —
  // description, the join rule, danger zone.
  const isRepo = ns?.kind === "code";
  // A repository's admins are its maintainers, and it is named by its label.
  const name = isRepo ? (label ?? namespace) : namespace;
  const adminWord = isRepo ? "maintainer" : "admin";
  // A token scoped per namespace with no direct grant here may be an admin the client cannot
  // see; a direct grant caps it.
  const needsAdmin = scopedUnseen(whoami, holdsDirectRow(rows, namespace))
    ? serverDecides(name)
    : isRepo
      ? needsMaintainer(name)
      : needsRole("admin", namespace);

  // Why the fields are locked. On a restricted row, from what its gate finds missing
  // (`restrictedMisses`, the same list `canUpdateNamespace` admits on), and while the grants
  // are unread, why.
  // Grants not read yet (or a failed read): say so, never a role the caller may well hold.
  // Before whoami loads, the grants alone settle nothing either: still loading.
  const unread = rows === null || !whoami;
  let lockedReason = unread ? unreadLine(mineLoading || !whoami, name) : needsAdmin;
  const restricted = ns?.classification === "restricted";
  // namespace.delete passes the restricted gate too, which an admin grant alone does not.
  const canDelete = restricted
    ? restrictedMisses(whoami, rows, { org, namespace }).length === 0
    : isNamespaceAdmin(whoami, rows, namespace);
  if (restricted && !unread) {
    const misses = restrictedMisses(whoami, rows, { org, namespace });
    const grant = rows.find((m) => m.namespace === namespace);
    const tokenMisses = [
      misses.includes("org") ? `be of ${org}` : null,
      misses.includes("name") ? `name ${namespace}` : null,
      misses.includes("claim") ? "carry an admin claim" : null,
    ].filter((p): p is string => p !== null);
    if (!misses.includes("grant") && tokenMisses.length > 0)
      lockedReason = `To change its settings, your token must ${andList(tokenMisses)}.`;
    // A token scoped per namespace: its role there is the server's to read.
    else if (!misses.includes("grant") && misses.includes("unseen")) lockedReason = serverDecides(name);
    // Both the token and the direct grant fall short: say both, whoever the caller is.
    else if (misses.includes("grant") && tokenMisses.length > 0)
      lockedReason = `To change its settings, your token must ${andList(tokenMisses)}, and you need a direct ${adminWord} grant on ${name}.`;
    // An admin by claim, by an inherited or pending grant, or by a scoped role the client
    // cannot see, whose token is complete, still lacks the direct grant the gate wants.
    else if (misses.includes("grant") && (hasAdminClaim(whoami) || grant?.role === "admin" || rolesUnseen(whoami)))
      lockedReason = `Changing its settings needs a direct ${adminWord} grant on ${name}.`;
  }

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
      toastError("Save failed", e);
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
      toastError("Delete failed", e);
    } finally {
      setSaving(false);
    }
  };

  const visibility = ns?.visibility || "private";
  const joinPolicy = ns?.join_policy || "inherit";
  const joinRules = isRepo ? REPO_JOIN_RULES : NAMESPACE_JOIN_RULES;
  const locked = !canManage || saving;

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={`Settings · ${namespace}`}
      className="w-full max-w-[600px]"
      onEscapeKeyDown={(e) => {
        // Escape while editing the cap restores it; the next Escape closes the dialog.
        if (capDraft === null) return;
        e.preventDefault();
        setCapEdit(null);
      }}
    >
      <div>
        {!ns && <Loading label="Loading namespace settings" />}
        {ns && (
          <div className="space-y-5">
            {/* The fields stay in view, locked, and say once what changing them needs. */}
            {!canManage && (
              <p className="m-0 text-xs text-text-muted" data-testid="settings-locked">
                {lockedReason}
              </p>
            )}
            <div className="flex items-end gap-2">
              <div className="min-w-0 flex-1">
                <TextField
                  id="ns-settings-description"
                  label="Description"
                  value={descDraft}
                  disabled={locked}
                  onChange={(value) => {
                    setDescDraft(value);
                    setDescDirty(value !== (ns.description || ""));
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && descDirty) save({ description: descDraft });
                  }}
                  placeholder="What is this namespace for?"
                />
              </div>
              {canManage && descDirty && (
                <Button
                  type="primary"
                  htmlType="button"
                  onClick={() => save({ description: descDraft })}
                  disabled={saving}
                >
                  Save
                </Button>
              )}
            </div>

            {!isRepo && (
              <>
                {/* A Select, not radios: each pick saves, and arrow keys in a radio group pick. */}
                <Select
                  label="Visibility"
                  value={visibility}
                  disabled={locked}
                  onSelect={(v) => {
                    const next = oneOf(["private", "public"] as const, v);
                    if (next && next !== visibility) save({ visibility: next });
                  }}
                  options={[
                    {
                      value: "private",
                      label: "Private",
                      description: "Only members you grant. Doesn't appear in Spaces.",
                    },
                    {
                      value: "public",
                      label: "Public",
                      description: "Anyone in the org can find + join this namespace via Spaces.",
                    },
                  ]}
                />

                <div>
                  <div className="flex items-end gap-2">
                    <div className="w-32">
                      <NumberField
                        id="ns-cost-cap"
                        label="Daily LLM budget (USD)"
                        step="0.01"
                        min="0"
                        value={capDraft ?? String(ns.daily_cost_cap_usd ?? 0)}
                        disabled={locked}
                        onChange={(value) => setCapEdit({ key, text: value })}
                        onBlur={() => void commitCap()}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") e.currentTarget.blur();
                        }}
                      />
                    </div>
                    <span className="pb-2 text-xs text-text-muted">
                      {(ns.daily_cost_cap_usd ?? 0) === 0 ? "inherits the global default" : "per 24h rolling window"}
                    </span>
                  </div>
                  <p className="mt-1.5 text-xs leading-relaxed text-text-muted">
                    Once this namespace's upstream LLM spend over the last 24h reaches the cap, background entity
                    extraction, code summaries and code-ingest descriptions skip, code.search skips query expansion, and
                    memory.ask, code.ask and knowledge.synthesize answer without synthesis until spend drops. Writes are
                    still accepted. 0 inherits the global default.
                  </p>
                </div>
              </>
            )}

            <div>
              <Select
                label="How people join"
                value={joinPolicy}
                disabled={locked}
                onSelect={(v) => {
                  const next = oneOf(
                    joinRules.map(([value]) => value),
                    v,
                  );
                  if (next && next !== joinPolicy) save({ join_policy: next });
                }}
                options={joinRules.map(([value, label, description]) => ({ value, label, description }))}
              />
              <p className="mt-1.5 text-xs text-text-muted">
                {isRepo ? (
                  <>
                    Right now this repository is{" "}
                    <span className="font-medium text-text">
                      {ns.effective_join_policy === "approval"
                        ? "restricted"
                        : ns.effective_join_policy || "restricted"}
                    </span>
                    . Every repository is listed and searchable once you read it; only the way in differs. Readers never
                    write; maintainers are named by an org admin.
                  </>
                ) : (
                  <>
                    Right now this namespace is{" "}
                    <span className="font-medium text-text">{ns.effective_join_policy || "approval"}</span>.
                  </>
                )}
              </p>
            </div>

            {!isRepo && (
              <>
                <div>
                  <Checkbox
                    label="Require approval for viewer (read-only) joins too"
                    aria-describedby="ns-requires-approval-hint"
                    checked={!!ns.requires_approval}
                    disabled={locked}
                    onCheckedChange={(c) => save({ requires_approval: c === true })}
                  />
                  <p id="ns-requires-approval-hint" className="mt-0.5 ml-6 text-xs text-text-muted">
                    Writer and admin joins always require approval. Turn this on if even read-only access should be
                    reviewed first.
                  </p>
                </div>

                {/* Prompt capture consent — opt IN. Consent is given, not withdrawn: a query is
                something a person typed, so capturing by default would collect everyone's until
                they found this and objected. Lives with the namespace because that is the
                per-person unit here. Written explicitly true/false rather than true/null so the
                namespace's answer always wins and an operator flipping the server default cannot
                silently re-enable someone. */}
                <div>
                  <Checkbox
                    label="Record what I search and what I store, for debugging"
                    aria-describedby="ns-capture-hint"
                    checked={ns.capture_generation_io === true}
                    disabled={locked}
                    onCheckedChange={(c) => save({ capture_generation_io: c === true })}
                  />
                  <p id="ns-capture-hint" className="mt-0.5 ml-6 text-xs text-text-muted">
                    Off unless you tick it, so none of your text is kept. Tick this and loom records your{" "}
                    <em>search wording</em> plus the memory text and conversation turns it sends to the model while
                    answering &mdash; which is what makes a bad recall diagnosable afterwards. Counts, cost, latency,
                    recall and quality scores are kept either way; those carry no text. Applies to this namespace only;
                    takes effect within a minute.
                  </p>
                </div>
              </>
            )}

            <div className="mt-2 border-t border-border pt-4">
              <div className="mb-2 text-xs font-medium text-danger">Danger zone</div>
              <div className="flex items-start justify-between gap-3">
                <p className="text-xs text-text-muted">
                  Delete deactivates the namespace. Existing data stays queryable for forensics. For full erasure run an
                  erasure.request separately.
                </p>
                <Unavailable reason={canDelete ? null : restricted || unread ? lockedReason : needsAdmin}>
                  <Button
                    type="danger"
                    htmlType="button"
                    iconLeft="trash"
                    onClick={onDelete}
                    disabled={saving}
                    className="shrink-0"
                  >
                    Delete namespace
                  </Button>
                </Unavailable>
              </div>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

/** "a", "a and b", "a, b and c". */
function andList(parts: string[]): string {
  return parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}` : parts[0];
}
