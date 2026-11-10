import { useEffect, useState } from "react";
import { deleteBrief, listBriefs, type BriefSummary } from "../api/briefs";
import { canCall } from "../lib/canCall";
import { useNamespaceRole } from "../lib/namespaceRole";
import { useStore } from "../lib/state";
import { confirmAction } from "./ConfirmDialog";
import { IconButton } from "./IconButton";
import { toastError } from "./Toast";
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
  const roleHere = useNamespaceRole(namespace);
  const whoami = useStore((s) => s.whoami);
  const canDelete = canCall("session.brief_delete", roleHere, whoami);
  const remove = async (b: BriefSummary) => {
    const ok = await confirmAction({
      title: `Forget the brief "${b.goal || b.brief_key}"?`,
      message: "A later session can no longer restore its work from it.",
      confirmLabel: "Forget",
      danger: true,
    });
    if (!ok) return;
    try {
      await deleteBrief({ org, namespace }, b.brief_key);
      setBriefs((all) => all.filter((x) => x.brief_key !== b.brief_key));
    } catch (e) {
      toastError("Couldn't forget the brief", e);
    }
  };
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
            <div key={b.brief_key} className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <SessionBriefPanel org={org} namespace={namespace} briefKey={b.brief_key} />
              </div>
              {canDelete && (
                <IconButton
                  icon="trash"
                  type="danger"
                  size="sm"
                  label={`Forget brief ${b.goal || b.brief_key}`}
                  tooltip="Forget"
                  onClick={() => void remove(b)}
                />
              )}
            </div>
          ))}
      </div>
    </Panel>
  );
}
