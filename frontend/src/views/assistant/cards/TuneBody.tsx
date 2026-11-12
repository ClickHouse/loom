import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import type { NamespaceTune, TuneProposal } from "../../../api";
import { registerCategory } from "../../../api/structure";
import { confirmAction } from "../../../components/ConfirmDialog";
import { CopyButton } from "../../../components/CopyButton";
import { toast, toastError } from "../../../components/Toast";
import { Unavailable } from "../../../components/ui";
import { GuideDialog } from "../../../flows/GuideDialog";
import { canWrite, needsRole, useNamespaceRoleRead } from "../../../lib/namespaceRole";
import { callReason } from "../../../lib/callReason";
import { useStore } from "../../../lib/state";
import { ReviewChanges } from "../ReviewChanges";

/** "Read again to apply": a card restored from History shows what was read then, so a draft
 *  is applied only from a read made now. */
export const staleReason = (readAt: number | undefined) =>
  `Read again to apply; this read is from ${readAt ? new Date(readAt).toLocaleString() : "an earlier visit"}.`;

/**
 * The tune card's body: the drafts namespace.tune returned, each with the person's own
 * control. A category is registered with one click behind a confirmation (namespace.register_
 * category, a writer's, whose register overwrites a category of the same name); a guide
 * draft opens in the guide editor on top of the guide read now; any other draft is copied,
 * and its "How to apply" says who applies it. The assistant applies nothing. The controls
 * write to the namespace the drafts were read in and follow the person's role there, not in
 * the header's.
 */
export function TuneBody({ tune, fresh, readAt }: { tune: NamespaceTune; fresh: boolean; readAt?: number }) {
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const roleRead = useNamespaceRoleRead(tune.namespace);
  const { role } = roleRead;
  const [guideDraft, setGuideDraft] = useState<TuneProposal | null>(null);
  if (!identity) return null;
  const scope = { org: identity.org, namespace: tune.namespace };
  const stale = fresh ? null : staleReason(readAt);

  const register = async (p: TuneProposal) => {
    const name = String(p.apply.args.name ?? "");
    const description = String(p.apply.args.description ?? "");
    const ok = await confirmAction({
      title: `Register the category ${name}?`,
      message: `Current: ${p.current || "(none)"}\nProposed: ${description}\nRegistering a name that exists replaces its description.`,
      confirmLabel: "Register",
    });
    if (!ok) return;
    try {
      const r = await registerCategory(scope, name, description);
      toast(
        r.embedded ? `Registered ${name}` : `Registered ${name}; category search finds it once it is embedded`,
        "ok",
      );
    } catch (e) {
      toastError("Registering the category failed", e);
    }
  };

  const actions = (p: TuneProposal) => {
    const a = p.apply;
    const op = (a.via ?? "op") === "op";
    if (op && a.op === "namespace.register_category" && a.args_complete)
      return (
        <Unavailable reason={stale ?? callReason("namespace.register_category", roleRead, whoami, tune.namespace)}>
          <Button type="secondary" htmlType="button" onClick={() => void register(p)}>
            Register this category
          </Button>
        </Unavailable>
      );
    if (op && a.op === "namespace.guide_set")
      return (
        <Unavailable reason={stale ?? (canWrite(role) ? null : needsRole("writer", tune.namespace))}>
          <Button type="secondary" htmlType="button" onClick={() => setGuideDraft(p)}>
            Review in the guide editor
          </Button>
        </Unavailable>
      );
    return <CopyButton text={JSON.stringify(a.args, null, 2)} label="Copy the change" className="self-start" />;
  };

  return (
    <>
      <ReviewChanges tune={tune} actions={actions} />
      {guideDraft && (
        <GuideDialog
          open
          onOpenChange={(v) => {
            if (!v) setGuideDraft(null);
          }}
          org={scope.org}
          namespace={scope.namespace}
          canEdit={canWrite(role)}
          initialContent={String(guideDraft.apply.args.content ?? guideDraft.proposed)}
          baseRevision={
            typeof guideDraft.apply.args.expected_revision === "number"
              ? guideDraft.apply.args.expected_revision
              : undefined
          }
          initialNote={String(guideDraft.apply.args.note ?? "")}
          onSaved={() => setGuideDraft(null)}
        />
      )}
    </>
  );
}
