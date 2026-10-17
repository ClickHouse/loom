import { useEffect, useState } from "react";
import { listBriefs, type BriefSummary } from "../api/briefs";
import { SectionHeading } from "./ui";
import { SessionBriefPanel } from "./SessionBriefPanel";

/**
 * The briefs this namespace holds, above its conversations: the way to find
 * a brief whose conversation was never recorded (a session run while
 * Recording was off, or on a client that does not record). Hidden when the
 * namespace holds none; each brief opens in place.
 */
export function SessionBriefsSection({ org, namespace }: { org: string; namespace: string }) {
  const [briefs, setBriefs] = useState<BriefSummary[]>([]);
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
    <div className="mb-6">
      <SectionHeading label="Session briefs" count={briefs.length} />
      <p className="text-xs text-text-muted mb-2">
        Where each coding session's work stood when it last compacted or ended. Open one to read it and copy what to say
        in Claude Code or Codex to restore it.
      </p>
      {briefs.map((b) => (
        <SessionBriefPanel key={b.brief_key} org={org} namespace={namespace} briefKey={b.brief_key} />
      ))}
    </div>
  );
}
