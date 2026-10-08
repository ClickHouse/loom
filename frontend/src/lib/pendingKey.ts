/** localStorage key of the pending-join cache behind the "approved by …" toast.
 *  Browse adds a pending request to it; the App bootstrap diffs it against
 *  `namespace.list_mine` on the next visit. The format lives in users'
 *  browsers: changing it orphans every cached pending request. */
export function pendingKey(subject: string | undefined, org: string): string {
  return `loom_pending_v1_${subject || org}`;
}
