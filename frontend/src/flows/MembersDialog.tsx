import { useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { IconButton as ClickIconButton } from "@clickhouse/click-ui/IconButton";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
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
import { needsMaintainer, scopedUnseen, serverDecides } from "../lib/callReason";
import { toast, toastError, toastErrorLine } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { IconButton } from "../components/IconButton";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { fieldError, ListTime, Loading, Unavailable } from "../components/ui";
import { ListState } from "../components/ui/ListState";
import { humanizeError } from "../lib/errorWords";
import { oneOf } from "../lib/oneOf";
import { isCollectionName } from "../lib/namespace";
import { allGrants, GRANTS_READ } from "../lib/grantPages";
import { fmtInt } from "../lib/format";
import { MORE_SHOWN, type ShowMoreRows, useShowMore } from "../lib/useShowMore";
import { grantSpellings, joined, mySpellings, namesMe, subjectLabel } from "../lib/grantSubject";

/** A row's Remove: the grant to everyone is no member, so removing it stops sharing. */
function removeLabel(subject: string, org: string): string {
  return subject === "everyone" ? `Stop sharing with everyone in ${org}` : `Remove ${subjectLabel(subject, org)}`;
}

/** A row's name: the subject as a person reads it, with the subject itself beneath when the
 *  two differ. */
function SubjectName({ r, me, org }: { r: GrantEntry; me: readonly string[]; org: string }) {
  const label = subjectLabel(r.subject, org);
  return (
    <>
      {/* Wrapped, not cut: a long subject (an email on a request) is read whole. */}
      <div className="break-all text-sm">
        {label}
        {namesMe(r, me) && <span className="ml-2 text-xs text-text-muted">(you)</span>}
      </div>
      {label !== r.subject && <div className="break-all font-mono text-xs text-text-muted">{r.subject}</div>}
    </>
  );
}

/** A members list drawn a step at a time: rows by subject, each focusing its first control,
 *  else itself. */
const MEMBER_ROWS: ShowMoreRows<GrantEntry> = {
  key: (r) => r.subject,
  rows: (list) =>
    [...(list?.querySelectorAll<HTMLLIElement>(":scope > li[data-subject]") ?? [])].map((li) => [
      li.dataset.subject ?? "",
      li.querySelector<HTMLElement>("button") ?? li,
    ]),
};

/** Show more under a list drawn a step at a time. */
function MoreButton({ label, onClick }: { label: string | null; onClick: () => void }) {
  if (!label) return null;
  return (
    <Button type="empty" htmlType="button" onClick={onClick}>
      {label}
    </Button>
  );
}

/** Who else can see a collection through one of the collections it sits in: read-only. */
export interface InheritedAccess {
  /** The parent's name, which keys its section; "" for the one entry that is no parent the
   *  person can open (one they cannot, or the parents when their grants failed to read). */
  namespace: string;
  /** The parent's title, or words for the "" entry. */
  title: string;
  /** Its grants; null when there are none to show (`note` says why). */
  rows: GrantEntry[] | null;
  /** More grants followed than were read. */
  cut?: boolean;
  /** Said in place of the rows when there are none to show; "Its members couldn't be read."
   *  when unset. */
  note?: string;
  /** Shows the parent in the Library. */
  onOpen?: () => void;
}

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  namespace: string;
  /** "code" switches the dialog to repository mode: members are readers (viewer only);
   *  maintainers (admin) are named by an org admin or a maintainer of it. */
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
  /** Whether the caller may change the members, when the page knows it: a collection's role
   *  from `collection.list`, the gate's own answer (it reaches through parents and groups,
   *  and no token claim reaches a collection). Unset: the token's claims and the caller's
   *  own grant decide. */
  manage?: boolean;
  /** A collection's parents, nearest first, and who can see it through each: listed under
   *  its own members, read-only, since they are changed on the parent. Null while they are
   *  read: the dialog says nothing of an empty list until it knows who else can see it. */
  inherited?: InheritedAccess[] | null;
  /** A line above the members that changes what they mean (sign-in off, say). */
  notice?: string;
  /** Where the focus goes as the dialog closes; unset, back to the control that opened it. */
  onCloseAutoFocus?: (e: Event) => void;
}

/** A collection holds documents: its roles say what they let one do with them. */
const COLLECTION_ROLES: Array<{ value: GrantRole; label: string; desc: string }> = [
  { value: "viewer", label: "Viewer", desc: "Read its documents" },
  { value: "writer", label: "Writer", desc: "Read and add documents" },
  { value: "admin", label: "Admin", desc: "Manage members and settings, and delete it" },
];

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
  manage,
  inherited = [],
  notice,
  onCloseAutoFocus,
}: Props) {
  const whoami = useStore((s) => s.whoami);
  const isCode = kind === "code";
  // A code index has two roles: readers (viewer) and maintainers (admin); an org admin or a
  // maintainer of it names a maintainer (the server enforces the ceiling — ROLE_CEILING).
  const roles = isCollectionName(namespace)
    ? COLLECTION_ROLES
    : isCode
      ? ROLES.filter((r) => r.value === "viewer" || r.value === "admin").map((r) =>
          r.value === "viewer"
            ? { ...r, label: "Reader", desc: "Read the index (read-only)" }
            : { ...r, label: "Maintainer", desc: "Approve readers, set how people join" },
        )
      : ROLES;
  const roleValues = roles.map((r) => r.value);
  const [rows, setRows] = useState<GrantEntry[] | null>(null);
  // More members followed than the pages read.
  const [cut, setCut] = useState(false);
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
    setCut(false);
    setLoadError(null);
  }

  useEffect(() => {
    if (!open) return;
    let live = true;
    // A read of many pages stops when the dialog closes, moves or reads again.
    const ctl = new AbortController();
    setRefreshing(true);
    setLoadError(null);
    allGrants({ org, namespace }, ctl.signal)
      .then((r) => {
        if (!live) return;
        setRows(r.rows);
        setCut(!!r.cut);
      })
      .catch((e) => {
        if (live) setLoadError(e);
      })
      .finally(() => {
        if (live) setRefreshing(false);
      });
    return () => {
      live = false;
      ctl.abort();
    };
  }, [open, org, namespace, reloadSeq]);

  // The person under every spelling a grant here or on a parent ties to whoami's.
  const me = mySpellings(whoami?.subject || "", [...(rows ?? []), ...(inherited ?? []).flatMap((p) => p.rows ?? [])]);
  const canManage =
    manage ??
    holdsAdminGrant(
      whoami,
      rows?.find((r) => namesMe(r, me)),
    );
  // Named by its label (a repository's "owner/name @ branch", a collection's title), else
  // by its address.
  const name = label ?? namespace;
  // A scoped token with no grant here may be an admin the client cannot see; a grant of its own caps it.
  const manageReason =
    canManage || rows === null
      ? null
      : manage === false
        ? needsRole("admin", name)
        : scopedUnseen(
              whoami,
              rows.some((r) => namesMe(r, me)),
            )
          ? serverDecides(name)
          : isCode
            ? needsMaintainer(name)
            : needsRole("admin", name);

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
      toast(`${subjectLabel(subject, org)} → ${role}`, "ok");
      load();
    } catch (e) {
      toastError("Update failed", e);
    } finally {
      setBusy(null);
    }
  };

  const onRemove = async (subject: string) => {
    // Who still lets them in: a grant to everyone here, else a parent whose grants name them
    // under any spelling, or name everyone.
    const theirs = grantSpellings(rows?.find((r) => r.subject === subject) ?? { subject });
    const reaches = (list: GrantEntry[] | null | undefined) =>
      !!list?.some(
        (x) =>
          x.status === "active" && (x.subject === "everyone" || grantSpellings(x).some((sp) => theirs.includes(sp))),
      );
    // Not the grant being removed: removing everyone leaves no grant to everyone here.
    const everyoneHere = !!rows?.some(
      (x) => x.status === "active" && x.subject === "everyone" && x.subject !== subject,
    );
    const through = (inherited ?? []).filter((p) => reaches(p.rows)).map((p) => p.title);
    const who = subjectLabel(subject, org);
    // The grant to everyone is no member: removing it narrows the collection to who is named.
    const toEveryone = subject === "everyone";
    const still = everyoneHere
      ? ` Everyone in ${org} can still see it.`
      : through.length
        ? ` ${toEveryone ? `Everyone in ${org} can` : "They can"} still see it through ${joined(through)}.`
        : "";
    if (
      !(await confirmAction({
        title: toEveryone ? `Stop sharing ${name} with everyone in ${org}?` : `Remove ${who} from ${name}?`,
        message: toEveryone
          ? through.length
            ? `The grant here is revoked.${still}`
            : "Only those named here or on a parent, and the members of groups named, keep access."
          : `Their grant here is revoked.${still}${
              // A namespace's gate also admits a token's own roles; no claim reaches a collection.
              isCollectionName(namespace) ? "" : " A token of theirs may still carry roles that let them in."
            }`,
        confirmLabel: toEveryone ? "Stop sharing" : "Remove",
        danger: true,
      }))
    )
      return;
    setBusy(subject);
    try {
      await revokeGrant({ org, namespace }, subject);
      toast(toEveryone ? `Stopped sharing with everyone in ${org}` : `Removed ${who}`, "ok");
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
    const who = subjectLabel(subject, org);
    if (ok === targets.length) {
      toast(targets.length > 1 ? `${verb} ${who} on ${ok} branches` : `${verb} ${who}`, "ok");
    } else if (ok === 0) {
      toastError(`Couldn't ${decision} ${who}`, firstError);
    } else {
      const msg = `${verb} ${who} on ${ok} of ${targets.length} branches; the rest failed: ${humanizeError(firstError, { bare: true })}`;
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
  const inheritedSeen = (inherited ?? []).some((p) => p.rows === null || p.rows.some((r) => r.status === "active"));
  // Up to GRANTS_READ grants are read, so the counts and "(you)" cover them; the rows are
  // drawn a step at a time.
  const members = useShowMore(activeRows, MORE_SHOWN, MEMBER_ROWS);
  const pending = useShowMore(pendingRows, MORE_SHOWN, MEMBER_ROWS);

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
      onCloseAutoFocus={onCloseAutoFocus}
    >
      <div data-testid="members-body" aria-busy={(rows !== null && refreshing) || undefined}>
        {notice && <p className="mb-3 text-sm text-text-muted">{notice}</p>}
        {rows === null && loadError === null && <Loading label={isCode ? "Loading readers" : "Loading members"} />}
        {loadError !== null && rows === null && (
          <ListState kind="error" things={isCode ? "readers" : "members"} error={loadError} onRetry={load} />
        )}
        {loadError !== null && rows !== null && (
          <div className="mb-3 text-xs text-text-muted" role="status">
            {`Couldn't refresh the ${isCode ? "readers" : "members"}; showing the last read.`}
          </div>
        )}
        {rows !== null && rows.length === 0 && inherited !== null && (
          <div className={`text-center text-sm text-text-muted ${inherited.length > 0 ? "py-3" : "py-8"}`}>
            {/* Seen through a parent only when one names someone, or may (unread or hidden). */}
            {inheritedSeen
              ? "No one is named here; who can see it comes from the collections it sits in."
              : "No members yet."}
            {canManage &&
              ` Click ${addLabel} below to ${inheritedSeen ? "name someone here" : "invite the first one"}.`}
          </div>
        )}
        {cut && (
          <p className="mb-3 text-xs text-text-muted">{`Only the first ${fmtInt(GRANTS_READ)} grants were read.`}</p>
        )}
        {pendingRows.length > 0 && (
          <div className="mb-4">
            <div className="mb-2 text-xs font-medium text-info">{`Pending requests (${fmtInt(pendingRows.length)})`}</div>
            <ul ref={pending.list} className="space-y-1">
              {pending.shown.map((r) => {
                const sinceDate = r.pending_since || r.granted_at;
                const hasBase = !!r.pending_role && r.role !== r.requested_role;
                const elsewhere = pendingElsewhere?.[r.subject] ?? [];
                const targets = targetsFor(r.subject).length;
                return (
                  <li
                    key={`p-${r.subject}`}
                    data-subject={r.subject}
                    tabIndex={-1}
                    className="flex items-center gap-3 rounded-lg border border-info/40 bg-info/10 p-2.5"
                  >
                    <RoleIcon role={r.requested_role} />
                    <div className="min-w-0 flex-1">
                      <SubjectName r={r} me={me} org={org} />
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
                    {!namesMe(r, me) || canManage ? (
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
                              title: `Withdraw your ${r.requested_role} request on ${name}?`,
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
                  </li>
                );
              })}
            </ul>
            <MoreButton label={pending.moreLabel} onClick={pending.showMore} />
          </div>
        )}
        {activeRows.length > 0 && (
          <div className="space-y-1">
            {(pendingRows.length > 0 || activeRows.length > MORE_SHOWN) && (
              <div className="mb-1 text-xs font-medium text-text-muted">
                {`${isCode ? "Readers" : "Members"} (${fmtInt(activeRows.length)})`}
              </div>
            )}
            <ul ref={members.list} className="space-y-1">
              {members.shown.map((r) => (
                <li
                  key={r.subject}
                  data-subject={r.subject}
                  tabIndex={-1}
                  className="flex items-center gap-3 rounded-lg p-2.5 hover:bg-surface-2"
                >
                  <RoleIcon role={r.role} />
                  <div className="min-w-0 flex-1">
                    <SubjectName r={r} me={me} org={org} />
                    <div className="text-xs text-text-muted">
                      Granted <ListTime time={r.granted_at} quiet /> by {r.granted_by}
                    </div>
                  </div>
                  {canManage && !namesMe(r, me) ? (
                    <>
                      <div className="w-36 shrink-0">
                        <Select
                          triggerProps={{ "aria-label": `Role for ${subjectLabel(r.subject, org)}` }}
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
                        label={removeLabel(r.subject, org)}
                        tooltip={r.subject === "everyone" ? removeLabel(r.subject, org) : `Remove from ${name}`}
                        onClick={() => onRemove(r.subject)}
                        disabled={busy === r.subject}
                      />
                    </>
                  ) : (
                    <>
                      <span className="px-2 text-xs capitalize text-text-muted">{r.role}</span>
                      {manageReason && !namesMe(r, me) && (
                        <Unavailable reason={manageReason}>
                          <ClickIconButton
                            type="danger"
                            size="sm"
                            icon="trash"
                            aria-label={removeLabel(r.subject, org)}
                          />
                        </Unavailable>
                      )}
                    </>
                  )}
                </li>
              ))}
            </ul>
            <MoreButton label={members.moreLabel} onClick={members.showMore} />
          </div>
        )}
        {inherited === null ? (
          <Loading label="Loading who else can see it" />
        ) : (
          inherited.map((p) => <InheritedSection key={p.namespace} access={p} me={me} org={org} />)
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

/** "Also can see it, through <parent>": the parent's active members, read-only, and its link. */
function InheritedSection({ access, me, org }: { access: InheritedAccess; me: readonly string[]; org: string }) {
  const active = (access.rows ?? []).filter((r) => r.status === "active");
  const heading = `Also can see it, through ${access.title}`;
  const rows = useShowMore(active, MORE_SHOWN, MEMBER_ROWS);
  return (
    <section aria-label={heading} className="mt-4 border-t border-border pt-3">
      <div className="mb-1 text-xs font-medium text-text-muted">
        {"Also can see it, through "}
        {access.onOpen ? (
          <Link component="button" type="button" size="sm" onClick={access.onOpen}>
            {access.title}
          </Link>
        ) : (
          access.title
        )}
      </div>
      {access.rows === null ? (
        <p className="py-1 text-sm text-text-muted">{access.note ?? "Its members couldn't be read."}</p>
      ) : active.length === 0 ? (
        <p className="py-1 text-sm text-text-muted">No one is named there.</p>
      ) : (
        <div className="space-y-1">
          {active.length > MORE_SHOWN && (
            <div className="text-xs text-text-muted">{`Members there (${fmtInt(active.length)})`}</div>
          )}
          <ul ref={rows.list} className="space-y-1">
            {rows.shown.map((r) => (
              <li
                key={r.subject}
                data-subject={r.subject}
                tabIndex={-1}
                className="flex items-center gap-3 rounded-lg p-2.5"
              >
                <RoleIcon role={r.role} />
                <div className="min-w-0 flex-1">
                  <SubjectName r={r} me={me} org={org} />
                </div>
                <span className="px-2 text-xs capitalize text-text-muted">{r.role}</span>
              </li>
            ))}
          </ul>
          <MoreButton label={rows.moreLabel} onClick={rows.showMore} />
        </div>
      )}
      {access.cut && (
        <p className="text-xs text-text-muted">{`Only the first ${fmtInt(GRANTS_READ)} grants there were read.`}</p>
      )}
    </section>
  );
}

function RoleIcon({ role }: { role: GrantRole }) {
  if (role === "admin") return <Icon name="star" size="xs" color="var(--global-color-text-muted)" aria-label="Admin" />;
  if (role === "writer") return <Icon name="user" size="xs" color="var(--color-text-muted)" aria-label="Writer" />;
  return <Icon name="eye" size="xs" color="var(--color-text-muted)" aria-label="Viewer" />;
}
