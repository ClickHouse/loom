import { Link } from "@clickhouse/click-ui/Link";
import { useStore } from "../lib/state";
import type { View } from "../lib/state";
import { kindOf } from "../lib/namespace";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "./ScopeBadge";
import { EmptyState } from "./EmptyState";
import { PageHeader } from "./ui";

/**
 * The page a memory view renders when the picked namespace is not a memory
 * namespace: a code index or a tracker namespace (reference data), or nothing at all. The
 * view itself never mounts, so no memory.* read is made against a namespace that holds no
 * memories — the dashboard makes no request the token cannot make.
 *
 * Every memory view keeps its own H1 text here, so the page is still the
 * page the sidebar named; the ScopeBadge under it says which namespace is
 * picked and of what kind.
 */

const VIEW_TITLE: Partial<Record<View, string>> = {
  memories: "Memories",
  recall: "Recall",
  profile: "Profile",
  sharing: "Flow",
  sessions: "Conversations",
  runs: "Runs",
};

export function MemoryScopeMissing({ view }: { view: View }) {
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  const mine = useStore((s) => s.myNamespaces);
  const setView = useStore((s) => s.setView);
  const navigate = useStore((s) => s.navigate);
  const openNamespacePicker = useStore((s) => s.openNamespacePicker);
  const title = VIEW_TITLE[view] ?? "Memories";
  const kind = kindOf(namespace, mine);

  /** Open the Graph view in Patterns mode on this tracker namespace, as a new history
   *  entry: Back returns here. */
  const openPatterns = () => navigate("graph", { query: { mode: "patterns" } });

  let body;
  if (!namespace) {
    body = (
      <EmptyState
        icon="cards"
        title="No memory namespace to read"
        description={`${title} reads a memory namespace, and your token reaches none yet. Join one from Spaces (namespace picker → Browse and join), or ask for a grant; anything you can already reach is in the namespace menu.`}
        action={{ label: "Browse and join spaces", onClick: () => setView("browse") }}
      />
    );
  } else if (kind === "qa") {
    body = (
      <EmptyState
        icon="flag"
        title={`${namespace} is a tracker source`}
        description={`It holds issues, pull requests and incidents read live from the tracker, not memories — ${title} has nothing to read there. Its bug board is the Graph view's Patterns mode.`}
        action={{ label: "Open in Patterns", onClick: openPatterns }}
      />
    );
  } else {
    body = (
      <EmptyState
        icon="code"
        title={`${namespace} is a repository index`}
        description={`It holds indexed code, not memories — ${title} has nothing to read there. Search and browse the repository from Repositories.`}
        action={{ label: "Open repositories", onClick: () => setView("code") }}
      />
    );
  }

  return (
    <PageContainer>
      <PageHeader title={title}>
        <ScopeBadge kind="namespace" value={namespace} />
      </PageHeader>
      {body}
      <p className="text-center text-sm text-text-muted">
        Reading memories? Pick a memory namespace from the{" "}
        <Link component="button" type="button" onClick={() => openNamespacePicker(true)}>
          namespace menu
        </Link>{" "}
        in the header.
      </p>
    </PageContainer>
  );
}
