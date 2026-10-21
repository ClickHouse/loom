import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { getNamespaceGuide, type Identity, type NamespaceGuide } from "../api";
import { GuideDialog } from "../flows/GuideDialog";
import { relativeTime } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { canWrite, useNamespaceRole } from "../lib/namespaceRole";
import { Panel } from "./ui";

/**
 * The namespace's guide where its memories are worked on: its summary and who last
 * changed it, one click from the whole text. With no guide, a writer is asked to write
 * one; a reader sees nothing, since there is nothing to read.
 */
export function NamespaceGuideCard({ identity }: { identity: Pick<Identity, "org" | "namespace"> }) {
  const role = useNamespaceRole(identity.namespace);
  const writer = canWrite(role);
  const [guide, setGuide] = useState<NamespaceGuide | null>(null);
  const [open, setOpen] = useState(false);
  const [reads, setReads] = useState(0);

  useAbortableEffect(
    async (signal, live) => {
      setGuide(null);
      try {
        const g = await getNamespaceGuide(identity.org, identity.namespace, { signal });
        if (live()) setGuide(g);
      } catch {
        // The card is a pointer, not the page: a failed read shows nothing.
      }
    },
    [identity.org, identity.namespace, reads],
  );

  if (guide === null || (!guide.found && !writer)) return null;
  const by = guide.updated_by ? ` by ${guide.updated_by}` : "";
  const when = guide.updated_at ? `, ${relativeTime(guide.updated_at)}` : "";

  return (
    <Panel as="section" aria-label="Namespace guide" className="mb-3" padding="sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-2 py-1">
        <div className="min-w-0 flex-1 basis-64">
          {guide.found ? (
            <>
              <p className="truncate text-sm text-text">
                <span className="font-semibold">Guide</span>
                {guide.summary ? ` · ${guide.summary}` : ""}
              </p>
              <p className="text-xs text-text-muted">
                Revision {guide.revision}
                {by}
                {when}. Agents read it when a session starts.
              </p>
            </>
          ) : (
            <>
              <p className="text-sm text-text">
                <span className="font-semibold">No guide yet</span>
              </p>
              <p className="text-xs text-text-muted">
                Say what this namespace holds, its key layout and what its tags mean. Agents read it when a session
                starts, and people find it in Spaces.
              </p>
            </>
          )}
        </div>
        <Button type="secondary" htmlType="button" iconLeft="info-in-circle" onClick={() => setOpen(true)}>
          {guide.found ? "Open guide" : "Write the guide"}
        </Button>
      </div>
      {open && (
        <GuideDialog
          open={true}
          onOpenChange={setOpen}
          org={identity.org}
          namespace={identity.namespace}
          canEdit={writer}
          startEditing={!guide.found}
          onSaved={() => setReads((n) => n + 1)}
        />
      )}
    </Panel>
  );
}
