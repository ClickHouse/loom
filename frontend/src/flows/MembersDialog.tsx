import { useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { IconButton as ClickIconButton } from "@clickhouse/click-ui/IconButton";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  listGrants,
  createGrant,
  revokeGrant,
  approveGrant,
  cancelNamespaceRequest,
  type GrantEntry,
  type GrantRole,
} from "../api";
import { useStore } from "../lib/state";
import { holdsAdminGrant } from "../lib/role";
import { needsRole } from "../lib/namespaceRole";
import { toast, toastError, toastErrorLine } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { IconButton } from "../components/IconButton";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { fieldError, ListTime, Loading, Unavailable } from "../components/ui";
import { ListState } from "../components/ui/ListState";
import { humanizeError } from "../lib/errorWords";
import { oneOf } from "../lib/oneOf";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  namespace: string;
  /** "code" switches the dialog to repository mode: members are readers (viewer only);
   *  maintainers (admin) are named by an org admin. */
  kind?: string;
  /** A human name for the namespace — for a code index "owner/name @ branch"
   *  — shown in the heading with the identifier beneath it, so the approver
   *  reads where access is being asked for (nothing in the heading truncates). */
  label?: string;
  /** The same requester's pending requests on the other branches of this repository, by
   *  subject. Approve / Reject here applies to every one of them in one go — one request
   *  per person and repository is what the approver decides on, never a branch at a time.
   *  */
  pendingElsewhere?: Record<string, { namespace: string; label: string }[]>;
  /** Fires after a member is added — the Setup checklist ticks "Invite a
   * teammate" on it. */
  onChanged?: () => void;
  /** The caller is known to be an admin here though no grant row names them: a
   *  collection's role reaches through its parents and the caller's groups. */
  admin?: boolean;
}

const ROLES: Array<{ value: GrantRole; label: string; desc: string }> = [
  { value: "viewer", label: "Viewer", desc: "Read only" },
  { value: "writer", label: "Writer", desc: "Read and contribute memories" },
  { value: "admin", label: "Admin", desc: "Manage members + settings + delete" },
];

export function MembersDialog({
  open,
  onOpenChange,
  org,
  namespace,
  kind,
  label,
  pendingElsewhere,
  onChanged,
  admin = false,
}: Props) {
  const whoami = useStore((s) => s.whoami);
  const isCode = kind === "code";
  // A code index has two roles: readers (viewer) and maintainers (admin),
  // and only an org admin may name a maintainer (the server enforces the
  // same ceiling — ROLE_CEILING).
  // Any maintainer may name another maintainer.
  const roles = isCode
    ? ROLES.filter((r) => r.value === "viewer" || r.value === "admin").map((r) =>
        r.value === "viewer"
          ? { ...r, label: "Reader", desc: "Read the index (read-only)" }
          : { ...r, label: "Maintainer", desc: "Approve readers, set how people join" },
      )
    : ROLES;
  const roleValues = roles.map((r) => r.value);
  const [rows, setRows] = useState<GrantEntry[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [newSubject, setNewSubject] = useState("");
  // A code index has no writer role, so its default must be one the select offers.
  const defaultRole: GrantRole = isCode ? "viewer" : "writer";
  const [newRole, setNewRole] = useState<GrantRole>(defaultRole);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // Bumped after a mutation to re-read the grants; the effect owns every load, so a
  // response for a namespace the dialog has left is dropped instead of shown.
  const [reloadSeq, setReloadSeq] = useState(0);
  const load = () => setReloadSeq((n) => n + 1);
  // The re-read after a change keeps the members on screen, marked busy, and so does a
  // failed one; a failed first read is said as one, with Retry, never as "no members".
  // Another namespace resets during render, so its members (and their actions) never
  // paint under the new one.
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<unknown>(null);
  const rowsKey = `${org}/${namespace}`;
  const [rowsFor, setRowsFor] = useState(rowsKey);
  if (rowsFor !== rowsKey) {
    setRowsFor(rowsKey);
    setRows(null);
    setLoadError(null);
  }

  useEffect(() => {
    if (!open) return;
    let live = true;
    setRefreshing(true);
    setLoadError(null);
    listGrants({ org, namespace })
      .then((r) => {
        if (live) setRows(r.rows || []);
      })
      .catch((e) => {
        if (live) setLoadError(e);
      })
      .finally(() => {
        if (live) setRefreshing(false);
      });
    return () => {
      live = false;
    };
  }, [open, org, namespace, reloadSeq]);

  const me = whoami?.subject || "";
  const canManage =
    admin ||
    holdsAdminGrant(
      whoami,
      rows?.find((r) => r.subject === me),
    );
  // What a member who is not an admin here sees on each control an admin uses: the control,
  // unavailable, with the role it needs. Null while the grants are unread.
  const manageReason = canManage || rows === null ? null : needsRole("admin", namespace);

  const onAdd = async () => {
    const subj = newSubject.trim();
    if (!subj) return;
    setBusy("__add__");
    setErr(null);
    try {
      await createGrant({ org, namespace }, subj, newRole);
      toast(`Added ${subj} as ${newRole}`, "ok");
      setNewSubject("");
      setNewRole(defaultRole);
      setAdding(false);
      load();
      onChanged?.();
    } catch (e) {
      setErr(humanizeError(e));
    } finally {
      setBusy(null);
    }
  };

  const onChangeRole = async (subject: string, role: GrantRole) => {
    setBusy(subject);
    try {
      // The role Select is the one deliberate "lower this member" control;
      // it says so to the server. onAdd never does.
      await createGrant({ org, namespace }, subject, role, { replace: true });
      toast(`${subject} → ${role}`, "ok");
      load();
    } catch (e) {
      toastError("Update failed", e);
    } finally {
      setBusy(null);
    }
  };

  const onRemove = async (subject: string) => {
    if (
      !(await confirmAction({
        title: `Remove ${subject} from ${namespace}?`,
        message:
          "Their JWT keeps any org-level roles, but the namespace-specific grant is revoked. They can re-request access later.",
        confirmLabel: "Remove",
        danger: true,
      }))
    )
      return;
    setBusy(subject);
    try {
      await revokeGrant({ org, namespace }, subject);
      toast(`Removed ${subject}`, "ok");
      load();
    } catch (e) {
      toastError("Remove failed", e);
    } finally {
      setBusy(null);
    }
  };

  /** Every namespace the decision applies to: this one plus the requester's
   * pending siblings (one decision per person and repository). */
  const targetsFor = (subject: string): { namespace: string; label: string }[] => [
    { namespace, label: label ?? namespace },
    ...(pendingElsewhere?.[subject] ?? []),
  ];

  const onApprove = async (subject: string, decision: "approve" | "reject") => {
    const targets = targetsFor(subject);
    setBusy(subject);
    let ok = 0;
    let firstError: unknown = null;
    for (const t of targets) {
      try {
        await approveGrant({ org, namespace: t.namespace }, subject, decision);
        ok++;
      } catch (e) {
        firstError ??= e;
      }
    }
    const verb = decision === "approve" ? "Approved" : "Rejected";
    if (ok === targets.length) {
      toast(targets.length > 1 ? `${verb} ${subject} on ${ok} branches` : `${verb} ${subject}`, "ok");
    } else if (ok === 0) {
      toastError(`Couldn't ${decision} ${subject}`, firstError);
    } else {
      const msg = `${verb} ${subject} on ${ok} of ${targets.length} branches; the rest failed: ${humanizeError(firstError, { bare: true })}`;
      toastErrorLine(msg, firstError);
    }
    setBusy(null);
    load();
    onChanged?.();
  };

  type PendingEntry = GrantEntry & { requested_role: GrantRole };
  const isPending = (r: GrantEntry) => r.status === "pending" || !!r.pending_role;
  const activeRows = (rows || []).filter((r) => !isPending(r));
  const pendingRows: PendingEntry[] = (rows || []).filter(isPending).map((r) => ({
    ...r,
    requested_role: r.pending_role || r.role,
  }));

  const addLabel = isCode ? "Add reader" : "Add member";

  return (
    <Modal
      open={open}
      onOpenChange={(v) => {
        if (!v) {
          setAdding(false);
          setErr(null);
        }
        onOpenChange(v);
      }}
      title={`${isCode ? "Readers of" : "Members of"} ${label ?? namespace}`}
      description={label ? namespace : undefined}
      className="w-full max-w-[600px]"
    >
      <div data-testid="members-body" aria-busy={(rows !== null && refreshing) || undefined}>
        {rows === null && loadError === null && <Loading label={isCode ? "Loading readers" : "Loading members"} />}
        {loadError !== null && rows === null && (
          <ListState kind="error" things={isCode ? "readers" : "members"} error={loadError} onRetry={load} />
        )}
        {loadError !== null && rows !== null && (
          <div className="mb-3 text-xs text-text-muted" role="status">
            {`Couldn't refresh the ${isCode ? "readers" : "members"}; showing the last read.`}
          </div>
        )}
        {rows !== null && rows.length === 0 && (
          <div className="py-8 text-center text-sm text-text-muted">
            No members yet.{canManage && ` Click ${addLabel} below to invite the first one.`}
          </div>
        )}
        {pendingRows.length > 0 && (
          <div className="mb-4">
            <div className="mb-2 text-xs font-medium text-info">Pending requests ({pendingRows.length})</div>
            <div className="space-y-1">
              {pendingRows.map((r) => {
                const sinceDate = r.pending_since || r.granted_at;
                const hasBase = !!r.pending_role && r.role !== r.requested_role;
                const elsewhere = pendingElsewhere?.[r.subject] ?? [];
                const targets = targetsFor(r.subject).length;
                return (
                  <div
                    key={`p-${r.subject}`}
                    className="flex items-center gap-3 rounded-lg border border-info/40 bg-info/10 p-2.5"
                  >
                    <RoleIcon role={r.requested_role} />
                    <div className="min-w-0 flex-1">
                      <div className="break-all font-mono text-sm">{r.subject}</div>
                      <div className="text-xs text-text-muted">
                        {hasBase ? (
                          <>
                            currently <span className="font-medium">{r.role}</span> · requesting{" "}
                            <span className="font-medium">{r.requested_role}</span>
                          </>
                        ) : (
                          <>
                            requested <span className="font-medium">{r.requested_role}</span>
                          </>
                        )}{" "}
                        · <ListTime time={sinceDate} quiet />
                        {elsewhere.length > 0 && (
                          <div className="mt-0.5 break-words">
                            also waiting on {elsewhere.map((t) => t.label).join(", ")} — one decision covers all
                          </div>
                        )}
                      </div>
                    </div>
                    {r.subject !== me || canManage ? (
                      <>
                        <Unavailable
                          reason={manageReason}
                          tip={targets > 1 ? `Also approves: ${elsewhere.map((t) => t.label).join(", ")}` : undefined}
                        >
                          <Button
                            type="secondary"
                            htmlType="button"
                            onClick={() => onApprove(r.subject, "approve")}
                            disabled={busy === r.subject}
                          >
                            {`Approve as ${isCode ? (r.requested_role === "admin" ? "maintainer" : "reader") : r.requested_role}${targets > 1 ? ` on ${targets} branches` : ""}`}
                          </Button>
                        </Unavailable>
                        <Unavailable reason={manageReason}>
                          <Button
                            type="empty"
                            htmlType="button"
                            onClick={() => onApprove(r.subject, "reject")}
                            disabled={busy === r.subject}
                          >
                            Reject
                          </Button>
                        </Unavailable>
                      </>
                    ) : (
                      <Button
                        type="secondary"
                        htmlType="button"
                        onClick={async () => {
                          if (
                            !(await confirmAction({
                              title: `Withdraw your ${r.requested_role} request on ${namespace}?`,
                              message: "Keeps any current access; the request just leaves the admin queue.",
                              confirmLabel: "Withdraw",
                            }))
                          )
                            return;
                          setBusy(r.subject);
                          try {
                            await cancelNamespaceRequest(org, namespace);
                            toast(`Withdrew ${r.requested_role} request`, "ok");
                            load();
                          } catch (e) {
                            toastError("Cancel failed", e);
                          } finally {
                            setBusy(null);
                          }
                        }}
                        disabled={busy === r.subject}
                      >
                        Withdraw
                      </Button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {activeRows.length > 0 && (
          <div className="space-y-1">
            {pendingRows.length > 0 && (
              <div className="mb-1 text-xs font-medium text-text-muted">Members ({activeRows.length})</div>
            )}
            {activeRows.map((r) => (
              <div key={r.subject} className="flex items-center gap-3 rounded-lg p-2.5 hover:bg-surface-2">
                <RoleIcon role={r.role} />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-sm">
                    {r.subject}
                    {r.subject === me && <span className="ml-2 font-sans text-xs text-text-muted">(you)</span>}
                  </div>
                  <div className="text-xs text-text-muted">
                    Granted <ListTime time={r.granted_at} quiet /> by {r.granted_by}
                  </div>
                </div>
                {canManage && r.subject !== me ? (
                  <>
                    <div className="w-36 shrink-0">
                      <Select
                        triggerProps={{ "aria-label": `Role for ${r.subject}` }}
                        value={r.role}
                        disabled={busy === r.subject}
                        onSelect={(value) => {
                          const role = oneOf(roleValues, value);
                          if (role !== null && role !== r.role) onChangeRole(r.subject, role);
                        }}
                        options={roles.map((opt) => ({ value: opt.value, label: opt.label }))}
                      />
                    </div>
                    <IconButton
                      icon="trash"
                      type="danger"
                      size="sm"
                      label={`Remove ${r.subject}`}
                      tooltip="Remove from namespace"
                      onClick={() => onRemove(r.subject)}
                      disabled={busy === r.subject}
                    />
                  </>
                ) : (
                  <>
                    <span className="px-2 text-xs capitalize text-text-muted">{r.role}</span>
                    {manageReason && r.subject !== me && (
                      <Unavailable reason={manageReason}>
                        <ClickIconButton type="danger" size="sm" icon="trash" aria-label={`Remove ${r.subject}`} />
                      </Unavailable>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {canManage && adding && (
        <div className="mt-4 space-y-2 border-t border-border pt-4">
          <div className="flex items-start gap-2">
            <div className="min-w-0 flex-1">
              <TextField
                aria-label="Login handle, verified email or subject of the member to add"
                value={newSubject}
                onChange={(value) => setNewSubject(value)}
                placeholder="login handle (sign-in namespace), verified email or service-account subject"
                autoFocus
                onKeyDown={(e) => e.key === "Enter" && onAdd()}
                {...fieldError("add-member-error", err)}
              />
            </div>
            <div className="w-36 shrink-0">
              <Select
                triggerProps={{ "aria-label": "Role for the new member" }}
                value={newRole}
                onSelect={(value) => {
                  const role = oneOf(roleValues, value);
                  if (role !== null) setNewRole(role);
                }}
                options={roles.map((opt) => ({ value: opt.value, label: opt.label }))}
              />
            </div>
          </div>
          <p className="text-xs text-text-muted">{roles.find((r) => r.value === newRole)?.desc}</p>
        </div>
      )}

      {manageReason && (
        <ModalFooter>
          <Unavailable reason={manageReason}>
            <Button type="primary" htmlType="button" iconLeft="plus">
              {addLabel}
            </Button>
          </Unavailable>
        </ModalFooter>
      )}
      {canManage && (
        <ModalFooter>
          {adding ? (
            <>
              <Button
                type="secondary"
                htmlType="button"
                onClick={() => {
                  setAdding(false);
                  setNewSubject("");
                  setErr(null);
                }}
              >
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="button"
                iconLeft="plus"
                onClick={onAdd}
                disabled={!newSubject.trim()}
                loading={busy === "__add__"}
              >
                Add
              </Button>
            </>
          ) : (
            <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAdding(true)}>
              {addLabel}
            </Button>
          )}
        </ModalFooter>
      )}
    </Modal>
  );
}

function RoleIcon({ role }: { role: GrantRole }) {
  if (role === "admin") return <Icon name="star" size="xs" color="var(--global-color-text-muted)" aria-label="Admin" />;
  if (role === "writer") return <Icon name="user" size="xs" color="var(--color-text-muted)" aria-label="Writer" />;
  return <Icon name="eye" size="xs" color="var(--color-text-muted)" aria-label="Viewer" />;
}
