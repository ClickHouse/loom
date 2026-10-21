import { Button } from "@clickhouse/click-ui/Button";
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
 * hands the focus to the view's heading, as a navigation does. Its name says where it
 * continues; its label stays one word, since a namespace name can be longer than a phone's
 * width and Click UI's button does not wrap.
 *
 * When the membership read failed (no rows), the account's access is unknown, not refused:
 * the page says it could not be checked and offers Retry.
 */
export function NamespaceNoAccess({ namespace }: { namespace: string }) {
  const identity = useStore((s) => s.identity);
  const unchecked = useStore((s) => s.myNamespaces === null);
  const pending = useStore((s) => !!s.myNamespaces?.some((m) => m.namespace === namespace && m.status === "pending"));
  const setIdentity = useStore((s) => s.setIdentity);
  const setView = useStore((s) => s.setView);
  const openNamespacePicker = useStore((s) => s.openNamespacePicker);
  const current = identity?.namespace ?? "";

  const why = unchecked
    ? "Loom could not read your memberships just now."
    : pending
      ? "It opens once one of its admins approves your request."
      : "Ask one of its admins for a grant, or join a public namespace from Spaces.";
  return (
    <PageContainer>
      <PageHeader title={unchecked ? `Couldn't check your access to ${namespace}` : `No access to ${namespace}`} />
      <EmptyState
        icon={unchecked ? "warning" : pending ? "clock" : "lock"}
        title={
          unchecked
            ? "Try again in a moment"
            : pending
              ? "Your request to join is waiting for approval"
              : "Your account cannot read this namespace"
        }
        description={current ? `${why} Until then you can keep working in ${current}.` : why}
        action={
          unchecked || (identity && current)
            ? undefined
            : { label: "Browse and join spaces", onClick: () => setView("browse") }
        }
      >
        {(unchecked || (identity && current)) && (
          <div className="mt-6 flex flex-wrap justify-center gap-2">
            {unchecked && (
              <Button type="primary" htmlType="button" onClick={() => window.location.reload()}>
                Retry
              </Button>
            )}
            {identity && current && (
              <Button
                type={unchecked ? "secondary" : "primary"}
                htmlType="button"
                aria-label={`Continue in ${current}`}
                onClick={() => {
                  setIdentity(identity);
                  arriveAt();
                }}
              >
                Continue
              </Button>
            )}
          </div>
        )}
      </EmptyState>
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
