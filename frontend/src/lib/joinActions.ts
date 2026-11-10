import { joinNamespace, cancelNamespaceRequest, leaveNamespace, type JoinResponse } from "../api";
import { toast } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { humanizeError } from "./errorWords";

/**
 * One join, one wording.
 *
 * A code index and a tracker source are joined through the same ops —
 * `namespace.join`, `namespace.cancel_request`, `namespace.leave` — and a
 * person reads the same sentences whichever kind of card they clicked. The
 * Repositories view's `useJoinActions` (code facet) and `useRecordActions` (record
 * facet) both call these; the toast and confirm copy lives here and nowhere else.
 *
 * `JoinState` is where the caller stands on one namespace, derived from the
 * server's standing fields and nothing else:
 *   member   — a live grant (`my_status = member` / `member_role` set)
 *   pending  — a request on file
 *   open     — not joined; the rule admits a reader at once
 *   approval — not joined; a maintainer approves
 *   closed   — not joinable from here
 */
export type JoinState = "member" | "pending" | "open" | "approval" | "closed";
export type JoinPolicy = "open" | "approval" | "closed";

export function joinState(input: { member: boolean; pending: boolean; join_policy?: JoinPolicy | "" }): JoinState {
  if (input.member) return "member";
  if (input.pending) return "pending";
  if (input.join_policy === "closed") return "closed";
  return input.join_policy === "open" ? "open" : "approval";
}

/** What a join, request, withdraw or leave names: the namespace plus the
 *  words a person knows it by ("Acme/widgets @ main", "tracker-jira"). */
export interface JoinTarget {
  org: string;
  namespace: string;
  label: string;
}

export type JoinRole = "viewer" | "admin";

/** `namespace.join`; the toast says whether the reader is in or queued. */
export async function joinTarget(t: JoinTarget, role: JoinRole = "viewer"): Promise<JoinResponse | null> {
  try {
    const res = await joinNamespace(t.org, t.namespace, { role });
    if (res.status === "pending") {
      const n = (res.pending_admins || []).length;
      const what = role === "admin" ? "Maintainer role requested" : "Access requested";
      toast(`${what} for ${t.label}${n ? ` · ${n} maintainer${n === 1 ? "" : "s"} notified` : ""}`, "ok");
    } else {
      toast(`Joined ${t.label} · read-only`, "ok");
    }
    return res;
  } catch (e) {
    toast(humanizeError(e), "error");
    return null;
  }
}

/** Confirm, then `namespace.cancel_request`. Resolves true when withdrawn. */
export async function withdrawTarget(t: JoinTarget): Promise<boolean> {
  if (
    !(await confirmAction({
      title: `Withdraw your request for ${t.label}?`,
      message: "The maintainers stop seeing it. You can ask again any time.",
      confirmLabel: "Withdraw request",
    }))
  )
    return false;
  try {
    await cancelNamespaceRequest(t.org, t.namespace);
    toast(`Request withdrawn for ${t.label}`, "ok");
    return true;
  } catch (e) {
    toast(humanizeError(e), "error");
    return false;
  }
}

/** Confirm, then `namespace.leave`. Resolves true when left. */
export async function leaveTarget(t: JoinTarget): Promise<boolean> {
  if (
    !(await confirmAction({
      title: `Leave ${t.label}?`,
      message: "You stop seeing this index and new tokens no longer include it. You can join again any time.",
      confirmLabel: "Leave",
    }))
  )
    return false;
  try {
    await leaveNamespace(t.org, t.namespace);
    toast(`Left ${t.label}`, "ok");
    return true;
  } catch (e) {
    toast(humanizeError(e), "error");
    return false;
  }
}
