import { useId, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { getNamespaceGuide, type Identity, type NamespaceGuide } from "../api";
import { GuideDialog } from "../flows/GuideDialog";
import { IconButton } from "./IconButton";
import { useAbortableEffect } from "../lib/hooks";
import { isPersonalCollection } from "../lib/namespace";
import { canEditGuide, useNamespaceRoleRead } from "../lib/namespaceRole";
import { callReason } from "../lib/callReason";
import { useStore } from "../lib/state";
import { Tip } from "./ui";

/**
 * The namespace's guide, one button in the page header beside the page's primary action.
 * The button is there whatever the read answers, so nothing on the page moves when it
 * lands and focus has a place to come back to. Its description says what the guide is
 * (its summary, or that there is none); a writer of a namespace without one lands straight
 * in the editor.
 */
export function NamespaceGuideButton({
  identity,
  compact = false,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  /** An icon alone, named "Guide", where the title row has no room for a label (a phone). */
  compact?: boolean;
}) {
  // An end user's personal collection takes no guide, and its name says so at render: no
  // button, no read, and nothing moves for it.
  const personal = isPersonalCollection(identity.namespace);
  const roleRead = useNamespaceRoleRead(identity.namespace);
  const { role } = roleRead;
  const whoami = useStore((s) => s.whoami);
  const writer = canEditGuide(role, identity.namespace);
  // Edit stays in the dialog, unavailable, from the gate the tune card's guide draft uses.
  const editReason = personal ? null : callReason("namespace.guide_set", roleRead, whoami, identity.namespace);
  const describedBy = useId();
  const [guide, setGuide] = useState<NamespaceGuide | null>(null);
  const [open, setOpen] = useState(false);
  const [reads, setReads] = useState(0);

  // A new namespace starts unknown; a re-read after a save keeps the last answer shown.
  const scope = `${identity.org}/${identity.namespace}`;
  const [shownScope, setShownScope] = useState(scope);
  if (shownScope !== scope) {
    setShownScope(scope);
    setGuide(null);
  }
  useAbortableEffect(
    async (signal, live) => {
      if (personal) return;
      try {
        const g = await getNamespaceGuide(identity.org, identity.namespace, { signal });
        if (live()) setGuide(g);
      } catch {
        // The button still opens the guide, which says why it cannot be read.
      }
    },
    [identity.org, identity.namespace, personal, reads],
  );

  if (personal) return null;
  const about = describeGuide(guide, writer);

  return (
    <>
      {compact ? (
        <IconButton
          icon="info-in-circle"
          label="Guide"
          tooltip={about}
          type="secondary"
          aria-describedby={describedBy}
          onClick={() => setOpen(true)}
        />
      ) : (
        <Tip content={about}>
          <Button
            type="secondary"
            htmlType="button"
            iconLeft="info-in-circle"
            aria-describedby={describedBy}
            onClick={() => setOpen(true)}
          >
            Guide
          </Button>
        </Tip>
      )}
      <span id={describedBy} className="sr-only">
        {about}
      </span>
      {open && (
        <GuideDialog
          open={true}
          onOpenChange={setOpen}
          org={identity.org}
          namespace={identity.namespace}
          canEdit={writer}
          editReason={editReason}
          startEditing
          onSaved={() => setReads((n) => n + 1)}
        />
      )}
    </>
  );
}

/** What the button says the guide is: before the read answers, then its revision and
 *  summary, or that there is none (with what to do about it, to a writer). */
function describeGuide(guide: NamespaceGuide | null, writer: boolean): string {
  if (guide === null) return "How this namespace is laid out, for agents and people";
  if (guide.found) return `Revision ${guide.revision}${guide.summary ? `: ${guide.summary}` : ""}`;
  if (writer) return "No guide yet: write how this namespace is laid out, for agents and people";
  return "No guide yet";
}
