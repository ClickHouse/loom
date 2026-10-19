import { useEffect, useState } from "react";
import { listBriefs, type BriefSummary } from "../api/briefs";
import { useFold } from "../lib/folds";
import { Panel, SectionHeading } from "./ui";
import { SessionBriefPanel } from "./SessionBriefPanel";

/**
 * The briefs this namespace holds, in one bucket above its conversations: the way to
 * find a brief whose conversation was never recorded (a session run while Recording was
 * off, or on a client that does not record). Folded to its title until opened, so the
 * threads stay at the top of the page; the briefs are read only once it opens. Hidden
 * when the namespace holds none.
 */
export function SessionBriefsSection({ org, namespace }: { org: string; namespace: string }) {
  const [briefs, setBriefs] = useState<BriefSummary[]>([]);
  const [shown, toggleShown] = useFold("session-briefs");
  useEffect(() => {
    let live = true;
    setBriefs([]);
    listBriefs({ org, namespace }, { limit: 20 })
      .then((b) => live && setBriefs(b))
      .catch(() => live && setBriefs([]));
    return () => {
      live = false;
    };
  }, [org, namespace]);
  if (!briefs.length) return null;
  return (
    <Panel
      as="section"
      aria-labelledby="session-briefs-heading"
      className="mb-5"
      padding="none"
      bodyClassName={shown ? "p-4" : "px-4 py-2"}
    >
      <SectionHeading
        id="session-briefs-heading"
        level={2}
        label="Session briefs"
        count={briefs.length}
        hint="Where each coding session's work stood when it last compacted or ended. Open one to read it and copy what to say in Claude Code or Codex to restore it."
        fold={{ open: shown, onToggle: toggleShown, controls: "session-briefs-body" }}
      />
      <div id="session-briefs-body" hidden={!shown}>
        {shown &&
          briefs.map((b) => (
            <SessionBriefPanel key={b.brief_key} org={org} namespace={namespace} briefKey={b.brief_key} />
          ))}
      </div>
    </Panel>
  );
}
