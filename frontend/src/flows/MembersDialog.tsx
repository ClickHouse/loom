import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X, Users, Plus, Crown, UserRound, Eye, Trash2, AlertCircle } from "lucide-react";
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
import { toast } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { relativeTime, humanizeError } from "../lib/format";
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
}

const ROLES: Array<{ value: GrantRole; label: string; desc: string }> = [
  { value: "viewer", label: "Viewer", desc: "Read only" },
  { value: "writer", label: "Writer", desc: "Read and contribute memories" },
  { value: "admin", label: "Admin", desc: "Manage members + settings + delete" },
];

export function MembersDialog({ open, onOpenChange, org, namespace, kind, label, pendingElsewhere, onChanged }: Props) {
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

  useEffect(() => {
    if (!open) return;
    let live = true;
    setRows(null);
    listGrants({ org, namespace })
      .then((r) => {
        if (live) setRows(r.rows || []);
      })
      .catch(() => {
        if (live) setRows([]);
      });
    return () => {
      live = false;
    };
  }, [open, org, namespace, reloadSeq]);

  const me = whoami?.subject || "";
  const canManage = holdsAdminGrant(
    whoami,
    rows?.find((r) => r.subject === me),
  );

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
      // The role <select> is the one deliberate "lower this member" control;
      // it says so to the server. onAdd never does.
      await createGrant({ org, namespace }, subject, role, { replace: true });
      toast(`${subject} → ${role}`, "ok");
      load();
    } catch (e) {
      toast(`Update failed: ${humanizeError(e)}`, "error");
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
      toast(`Remove failed: ${humanizeError(e)}`, "error");
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
    } else {
      toast(`${verb} ${subject} on ${ok} of ${targets.length} branches — ${humanizeError(firstError)}`, "error");
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

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(v) => {
        if (!v) {
          setAdding(false);
          setErr(null);
        }
        onOpenChange(v);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(600px,92vw)] max-h-[85vh] flex flex-col rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
          <div className="flex items-start justify-between gap-3 px-6 py-4 border-b border-[var(--color-border)] shrink-0">
            <div className="min-w-0">
              <Dialog.Title className="text-lg font-semibold tracking-tight flex items-start gap-2 min-w-0">
                <Users size={16} className="shrink-0 mt-1" />
                <span className="break-words">
                  {isCode ? "Readers of" : "Members of"}{" "}
                  {label ? <span>{label}</span> : <span className="font-mono break-all">{namespace}</span>}
                </span>
              </Dialog.Title>
              {label && (
                <div className="mt-0.5 pl-6 text-2xs font-mono text-[var(--color-text-subtle)] break-all">
                  {namespace}
                </div>
              )}
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {canManage && !adding && (
                <button
                  onClick={() => setAdding(true)}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)]"
                >
                  <Plus size={13} /> {isCode ? "Add reader" : "Add member"}
                </button>
              )}
              <Dialog.Close
                className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                aria-label="Close"
              >
                <X size={16} />
              </Dialog.Close>
            </div>
          </div>

          <div className="px-6 py-4 flex-1 overflow-y-auto">
            {rows === null && <div className="text-sm text-[var(--color-text-muted)] text-center py-8">Loading…</div>}
            {rows !== null && rows.length === 0 && (
              <div className="text-sm text-[var(--color-text-muted)] text-center py-8">
                No members yet.{canManage && " Click Add member below to invite the first one."}
              </div>
            )}
            {pendingRows.length > 0 && (
              <div className="mb-4">
                <div className="text-2xs uppercase tracking-wider text-warn mb-2 font-semibold">
                  Pending requests ({pendingRows.length})
                </div>
                <div className="space-y-1">
                  {pendingRows.map((r) => {
                    const sinceDate = r.pending_since || r.granted_at;
                    const hasBase = !!r.pending_role && r.role !== r.requested_role;
                    return (
                      <div
                        key={`p-${r.subject}`}
                        className="flex items-center gap-3 p-2.5 rounded-lg bg-warn/10 border border-warn/40"
                      >
                        <RoleIcon role={r.requested_role} />
                        <div className="flex-1 min-w-0">
                          <div className="text-sm font-mono break-all">{r.subject}</div>
                          <div className="text-2xs text-[var(--color-text-muted)]">
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
                            · {relativeTime(sinceDate)}
                            {(pendingElsewhere?.[r.subject] ?? []).length > 0 && (
                              <div className="mt-0.5 break-words">
                                also waiting on {(pendingElsewhere?.[r.subject] ?? []).map((t) => t.label).join(", ")} —
                                one decision covers all
                              </div>
                            )}
                          </div>
                        </div>
                        {canManage ? (
                          <>
                            <button
                              onClick={() => onApprove(r.subject, "approve")}
                              disabled={busy === r.subject}
                              title={
                                targetsFor(r.subject).length > 1
                                  ? `Also approves: ${(pendingElsewhere?.[r.subject] ?? []).map((t) => t.label).join(", ")}`
                                  : undefined
                              }
                              className="px-2.5 py-1 text-xs font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-50"
                            >
                              Approve as{" "}
                              {isCode ? (r.requested_role === "admin" ? "maintainer" : "reader") : r.requested_role}
                              {targetsFor(r.subject).length > 1 && <> on {targetsFor(r.subject).length} branches</>}
                            </button>
                            <button
                              onClick={() => onApprove(r.subject, "reject")}
                              disabled={busy === r.subject}
                              className="px-2.5 py-1 text-xs text-[var(--color-text-muted)] hover:text-danger rounded-md disabled:opacity-50"
                            >
                              Reject
                            </button>
                          </>
                        ) : r.subject === me ? (
                          <button
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
                                toast(`Cancel failed: ${humanizeError(e)}`, "error");
                              } finally {
                                setBusy(null);
                              }
                            }}
                            disabled={busy === r.subject}
                            className="px-2.5 py-1 text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] disabled:opacity-50"
                          >
                            Withdraw
                          </button>
                        ) : (
                          <span className="text-2xs text-[var(--color-text-muted)] px-2">awaiting admin</span>
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
                  <div className="text-2xs uppercase tracking-wider text-[var(--color-text-subtle)] mb-1 font-semibold">
                    Members ({activeRows.length})
                  </div>
                )}
                {activeRows.map((r) => (
                  <div
                    key={r.subject}
                    className="flex items-center gap-3 p-2.5 rounded-lg hover:bg-[var(--color-surface-2)]"
                  >
                    <RoleIcon role={r.role} />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-mono truncate">
                        {r.subject}
                        {r.subject === me && (
                          <span className="ml-2 text-2xs text-[var(--color-text-subtle)] font-sans">(you)</span>
                        )}
                      </div>
                      <div className="text-2xs text-[var(--color-text-muted)]">
                        Granted {relativeTime(r.granted_at)} by {r.granted_by}
                      </div>
                    </div>
                    {canManage && r.subject !== me ? (
                      <>
                        <select
                          aria-label={`Role for ${r.subject}`}
                          value={r.role}
                          disabled={busy === r.subject}
                          onChange={(e) => {
                            const role = oneOf(roleValues, e.target.value);
                            if (role !== null) onChangeRole(r.subject, role);
                          }}
                          className="text-xs px-2 py-1 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]"
                        >
                          {roles.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                              {opt.label}
                            </option>
                          ))}
                        </select>
                        <button
                          onClick={() => onRemove(r.subject)}
                          disabled={busy === r.subject}
                          className="p-1.5 text-danger hover:bg-danger/15 rounded-md transition-colors disabled:opacity-50"
                          aria-label={`Remove ${r.subject}`}
                          title="Remove from namespace"
                        >
                          <Trash2 size={13} />
                        </button>
                      </>
                    ) : (
                      <span className="text-xs text-[var(--color-text-muted)] capitalize px-2">{r.role}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {canManage && (
            <div className="px-6 py-3.5 border-t border-[var(--color-border)] bg-[var(--color-surface-2)] rounded-b-2xl shrink-0">
              {!adding ? (
                <button
                  onClick={() => setAdding(true)}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)]"
                >
                  <Plus size={13} /> {isCode ? "Add reader" : "Add member"}
                </button>
              ) : (
                <div className="space-y-2">
                  <div className="flex items-center gap-2">
                    <input
                      aria-label="Email or subject of the member to add"
                      value={newSubject}
                      onChange={(e) => setNewSubject(e.target.value)}
                      placeholder="user@example.com (or subject identifier)"
                      autoFocus
                      onKeyDown={(e) => e.key === "Enter" && onAdd()}
                      className="flex-1 px-3 py-2 text-base rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]"
                    />
                    <select
                      aria-label="Role for the new member"
                      value={newRole}
                      onChange={(e) => {
                        const role = oneOf(roleValues, e.target.value);
                        if (role !== null) setNewRole(role);
                      }}
                      className="px-2 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]"
                    >
                      {roles.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="text-2xs text-[var(--color-text-muted)]">
                    {roles.find((r) => r.value === newRole)?.desc}
                  </div>
                  {err && (
                    <div className="flex gap-2 px-3 py-2 text-xs text-danger bg-danger/15 rounded-md">
                      <AlertCircle size={13} className="shrink-0 mt-0.5" />
                      <span>{err}</span>
                    </div>
                  )}
                  <div className="flex items-center justify-end gap-2">
                    <button
                      onClick={() => {
                        setAdding(false);
                        setNewSubject("");
                        setErr(null);
                      }}
                      className="px-3 py-1.5 text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={onAdd}
                      disabled={!newSubject.trim() || busy === "__add__"}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-50"
                    >
                      <Plus size={12} /> {busy === "__add__" ? "Adding…" : "Add"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function RoleIcon({ role }: { role: GrantRole }) {
  if (role === "admin") return <Crown size={13} className="text-warn shrink-0" aria-label="Admin" />;
  if (role === "writer")
    return <UserRound size={13} className="text-[var(--color-text-muted)] shrink-0" aria-label="Writer" />;
  return <Eye size={13} className="text-[var(--color-text-subtle)] shrink-0" aria-label="Viewer" />;
}
