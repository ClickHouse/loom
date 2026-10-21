import { Link } from "@clickhouse/click-ui/Link";
import { arriveAt } from "../lib/place";
import { useStore } from "../lib/state";
import { PageContainer } from "../layout/PageContainer";
import { EmptyState } from "./EmptyState";
import { PageHeader } from "./ui";

/**
 * The page a link to a namespace the account cannot read opens on, in place of any view.
 * Nothing of that namespace is read, and the namespace under the page (the one a visit
 * without the link opens, named in the header's picker) is never shown as if it were the
 * one the link asked for. Continue, a pick or any navigation leaves the page; Continue
 * hands the focus to the view's heading, as a navigation does.
 */
export function NamespaceNoAccess({ namespace }: { namespace: string }) {
  const identity = useStore((s) => s.identity);
  const pending = useStore((s) => !!s.myNamespaces?.some((m) => m.namespace === namespace && m.status === "pending"));
  const setIdentity = useStore((s) => s.setIdentity);
  const setView = useStore((s) => s.setView);
  const openNamespacePicker = useStore((s) => s.openNamespacePicker);
  const current = identity?.namespace ?? "";

  const why = pending
    ? "It opens once one of its admins approves your request."
    : "Ask one of its admins for a grant, or join a public namespace from Spaces.";
  return (
    <PageContainer>
      <PageHeader title={`No access to ${namespace}`} />
      <EmptyState
        icon={pending ? "clock" : "lock"}
        title={pending ? "Your request to join is waiting for approval" : "Your account cannot read this namespace"}
        description={current ? `${why} Until then you can keep working in ${current}.` : why}
        action={
          identity && current
            ? {
                label: "Continue",
                onClick: () => {
                  setIdentity(identity);
                  arriveAt();
                },
              }
            : { label: "Browse and join spaces", onClick: () => setView("browse") }
        }
      />
      <p className="text-center text-sm text-text-muted">
        Or pick another namespace from the{" "}
        <Link component="button" type="button" onClick={() => openNamespacePicker(true)}>
          namespace menu
        </Link>{" "}
        in the header.
      </p>
    </PageContainer>
  );
}
