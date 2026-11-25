import { useEffect, useState } from "react";
import { getBrief, type BriefGetResponse } from "../api/briefs";
import { readSessionHandoff, sessionHandoffUrl, type SessionHandoffRef } from "../lib/sessionHandoff";
import { useStore, viewHref } from "../lib/state";
import { CopyButton } from "./CopyButton";
import { Alert } from "./ui/Alert";
import { Loading } from "./ui/Loading";
import { SectionHeading } from "./ui/SectionHeading";

type BriefScope = { org: string; namespace: string };

export function briefHandoffUrl(scope: BriefScope, brief: BriefGetResponse): string | null {
  const ref = {
    ...scope,
    brief: brief.snapshot_key ?? "",
    revision: String(brief.revision),
    snapshot: brief.snapshot_id ?? "",
    commit: brief.head_sha,
  };
  const parsed = readSessionHandoff(new URLSearchParams(ref), scope.namespace);
  return parsed && parsed !== "invalid" ? sessionHandoffUrl(viewHref("sessions"), parsed) : null;
}

function HandoffError({ message }: { message: string }) {
  return <Alert state="warning" role="alert" text={message} />;
}

function HandoffPreview({ handoff }: { handoff: SessionHandoffRef }) {
  const { org, namespace, brief: key, revision, snapshot, commit } = handoff;
  const [result, setResult] = useState<BriefGetResponse | null>();
  useEffect(() => {
    let active = true;
    getBrief({ org, namespace }, key, { revision, snapshotId: snapshot })
      .then((brief) => {
        if (active)
          setResult(
            brief.found &&
              brief.snapshot_key === key &&
              brief.revision === revision &&
              brief.snapshot_id === snapshot &&
              brief.head_sha === commit
              ? brief
              : null,
          );
      })
      .catch(() => {
        if (active) setResult(null);
      });
    return () => {
      active = false;
    };
  }, [org, namespace, key, revision, snapshot, commit]);

  if (result === undefined) return <Loading label="Loading session handoff" />;
  if (!result) return <HandoffError message="This session handoff is unavailable." />;
  return (
    <section className="mb-4 space-y-3 rounded-sm border border-border bg-surface-2 p-4">
      <SectionHeading label="Session handoff" level={2} />
      <p className="break-all font-mono text-xs text-text-muted">Commit {commit}</p>
      <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words font-mono text-sm">
        {result.rendered}
      </pre>
      <CopyButton
        label="Copy restore prompt"
        text={`Restore the Loom session handoff <${sessionHandoffUrl(viewHref("sessions"), handoff)}>`}
        className="shrink-0"
      />
    </section>
  );
}

export function SessionHandoff({ identity }: { identity: BriefScope }) {
  const handoff = useStore((state) => state.sessionHandoff);
  if (!handoff) return null;
  if (handoff === "invalid") return <HandoffError message="This session handoff link is incomplete or invalid." />;
  if (handoff.org !== identity.org || handoff.namespace !== identity.namespace)
    return <HandoffError message="Open this session handoff in its organization and namespace." />;
  return <HandoffPreview key={JSON.stringify(handoff)} handoff={handoff} />;
}
