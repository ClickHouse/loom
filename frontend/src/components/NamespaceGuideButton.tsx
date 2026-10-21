import { useId, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { getNamespaceGuide, type Identity, type NamespaceGuide } from "../api";
import { GuideDialog } from "../flows/GuideDialog";
import { IconButton } from "./IconButton";
import { useAbortableEffect } from "../lib/hooks";
import { canWrite, useNamespaceRole } from "../lib/namespaceRole";
import { Tip } from "./ui";

/**
 * The namespace's guide, one button in the page header beside the page's primary action.
 * The button is always there, whatever the read answers, so nothing on the page moves when
 * it lands and focus has a place to come back to. Its description says what the guide is
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
  const role = useNamespaceRole(identity.namespace);
  const writer = canWrite(role);
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
      try {
        const g = await getNamespaceGuide(identity.org, identity.namespace, { signal });
        if (live()) setGuide(g);
      } catch {
        // The button still opens the guide, which says why it cannot be read.
      }
    },
    [identity.org, identity.namespace, reads],
  );

  const about = guide?.found
    ? `Revision ${guide.revision}${guide.summary ? `: ${guide.summary}` : ""}`
    : guide
      ? writer
        ? "No guide yet: write how this namespace is laid out, for agents and people"
        : "No guide yet"
      : "How this namespace is laid out, for agents and people";

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
          startEditing={writer && guide?.found === false}
          onSaved={() => setReads((n) => n + 1)}
        />
      )}
    </>
  );
}
