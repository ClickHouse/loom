import { useEffect, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import {
  listKnowledgeSources,
  parseKnowledgeSourceLinks,
  updateKnowledgeSource,
  type KnowledgeSourceParsedLink,
} from "../api/knowledgeSources";
import { committedAnyway, humanizeError } from "../lib/errorWords";
import { useMounted } from "../lib/hooks";
import { sourceItemText, sourceLinks, type KnowledgeSource } from "../lib/knowledgeSources";
import { useStore } from "../lib/state";
import { CancelButton, DialogFrame, ErrorNote } from "./TaskDialogFrame";
import { SourceLinkPreview, validateSourceLinkPreview } from "./SourceLinkPreview";

interface Props {
  org: string;
  namespace: string;
  source: KnowledgeSource;
  canManage: boolean;
  onClose: () => void;
  onUpdated: () => void;
  onCloseAutoFocus?: (event: Event) => void;
}

function actorScope() {
  const s = useStore.getState();
  return JSON.stringify([
    s.identity?.org,
    s.whoami?.org,
    s.whoami?.subject,
    s.whoami?.roles,
    s.whoami?.is_service_account,
    s.authed,
    s.sessionExpired,
  ]);
}

export function EditSourceItemsDialog(props: Props) {
  const scope = useStore(() => actorScope());
  const initial = useRef(scope);
  // The source's ownership projection belongs to the identity that loaded it.
  if (scope !== initial.current) return null;
  return (
    <EditSourceItems
      key={JSON.stringify([scope, props.org, props.namespace, props.source, props.canManage])}
      {...props}
    />
  );
}

function EditSourceItems({ org, namespace, source, canManage, onClose, onUpdated, onCloseAutoFocus }: Props) {
  const mounted = useMounted();
  const whoami = useStore((s) => s.whoami);
  const actor = actorScope();
  const current = () => mounted.current && actor === actorScope();
  const allowed =
    canManage &&
    whoami?.org === org &&
    (source.credential_mode !== "connected" || (!!whoami?.subject && source.owned_by_caller === true));
  const running = useRef(false);
  const previewRequest = useRef<AbortController | null>(null);
  useEffect(() => () => previewRequest.current?.abort(), []);
  const [linksText, setLinksText] = useState("");
  const [items, setItems] = useState(source.items ?? []);
  const [removed, setRemoved] = useState<number[]>([]);
  const [review, setReview] = useState<{ stamp: string; links: KnowledgeSourceParsedLink[]; valid: boolean } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"preview" | "save" | "reload" | null>(null);
  const [needsReload, setNeedsReload] = useState(false);
  const links = sourceLinks(linksText);
  const stamp = JSON.stringify([links, removed]);
  const reviewed = !needsReload && review?.stamp === stamp && review.valid;
  const selected = items.filter((_, index) => removed.includes(index));
  const changed = links.length > 0 || selected.length > 0;
  const limit = source.kind === "gdrive" ? 300 : 20;
  const problems = links.length > limit ? [`Choose at most ${limit} links.`] : [];
  if (
    links.some((link) => {
      try {
        return new URL(link).protocol !== "https:";
      } catch {
        return true;
      }
    })
  )
    problems.push("Use a complete HTTPS link for each item.");

  const run = async (save: boolean) => {
    if (!current() || !allowed || needsReload || running.current || !changed || problems.length || (save && !reviewed))
      return;
    running.current = true;
    setBusy(save ? "save" : "preview");
    setError(null);
    try {
      if (!save) {
        const controller = new AbortController();
        previewRequest.current = controller;
        const parsed = links.length
          ? (await parseKnowledgeSourceLinks({ org, namespace }, links, { signal: controller.signal })).links
          : [];
        if (!current()) return;
        const { matches, problems: failures } = validateSourceLinkPreview(parsed, source.kind, links);
        setReview(matches ? { stamp, links: parsed, valid: failures.length === 0 } : null);
        if (failures.length) {
          setError(failures.join(" "));
        }
      } else {
        await updateKnowledgeSource({ org, namespace }, source.name, {
          ...(links.length ? { add_links: links } : {}),
          ...(selected.length ? { remove_items: selected } : {}),
        });
        if (!current()) return;
        onUpdated();
        onClose();
      }
    } catch (e) {
      if (!current()) return;
      if (save && committedAnyway("Update source", e)) {
        onUpdated();
        onClose();
      } else {
        setReview(null);
        if (save) setNeedsReload(true);
        setError(humanizeError(e));
      }
    } finally {
      running.current = false;
      if (mounted.current) setBusy(null);
    }
  };

  const reloadItems = async () => {
    if (!current() || running.current) return;
    running.current = true;
    setBusy("reload");
    setError(null);
    const controller = new AbortController();
    previewRequest.current = controller;
    try {
      const result = await listKnowledgeSources({ org, namespace }, { signal: controller.signal });
      if (!current()) return;
      const fresh = result.sources.find((entry) => entry.name === source.name);
      if (
        !fresh ||
        fresh.kind !== source.kind ||
        (fresh.credential_mode ?? "shared") !== (source.credential_mode ?? "shared") ||
        (fresh.credential_mode === "connected" && fresh.owned_by_caller !== true)
      ) {
        setError("This source is no longer available to edit. Close this dialog and refresh sources.");
        return;
      }
      setItems(fresh.items ?? []);
      setRemoved([]);
      setReview(null);
      setNeedsReload(false);
    } catch (e) {
      if (current()) setError(humanizeError(e, { read: true }));
    } finally {
      running.current = false;
      if (mounted.current) setBusy(null);
    }
  };

  return (
    <DialogFrame
      open
      onCloseAutoFocus={onCloseAutoFocus}
      onOpenChange={(open) => {
        if (!open && busy !== "save") onClose();
      }}
      title={`Edit items · ${source.name}`}
      description="Add content links or remove selected items from this source."
      footer={
        <>
          <CancelButton disabled={busy === "save"} />
          <Button
            type="secondary"
            htmlType="button"
            loading={busy === "preview"}
            disabled={busy !== null || needsReload || !allowed || !changed || problems.length > 0}
            onClick={() => void run(false)}
          >
            Review changes
          </Button>
          <Button
            type="primary"
            htmlType="button"
            loading={busy === "save"}
            disabled={busy !== null || !allowed || !reviewed || problems.length > 0}
            onClick={() => void run(true)}
          >
            Save changes
          </Button>
        </>
      }
    >
      <p className="text-xs text-text-muted">
        These changes apply to the source's current items without replacing its other selections, account or access
        mode. Removing an item stops future imports from it. Documents held by another item or source remain.
      </p>
      {!allowed && <ErrorNote>You no longer have access to edit this source. Close it and refresh sources.</ErrorNote>}
      {needsReload && (
        <>
          <ErrorNote>
            Save could not be confirmed. Reload current items before trying again, then select removals and review any
            links you still want to add.
          </ErrorNote>
          <Button
            type="secondary"
            htmlType="button"
            loading={busy === "reload"}
            disabled={busy !== null || !allowed}
            onClick={() => void reloadItems()}
          >
            Reload current items
          </Button>
        </>
      )}
      <fieldset disabled={busy !== null || !allowed || needsReload} className="space-y-4" aria-label="Item changes">
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Select items to remove</legend>
          {items.length ? (
            items.map((item, index) => (
              <Checkbox
                key={JSON.stringify(item)}
                label={sourceItemText(item)}
                checked={removed.includes(index)}
                onCheckedChange={() =>
                  setRemoved((values) =>
                    values.includes(index) ? values.filter((value) => value !== index) : [...values, index],
                  )
                }
              />
            ))
          ) : (
            <p className="text-xs text-text-muted">Current items are not available. You can still add links.</p>
          )}
        </fieldset>
        <TextAreaField
          id="source-added-links"
          label="Content links to add (one per line)"
          rows={4}
          value={linksText}
          onChange={setLinksText}
          autoFocus
        />
      </fieldset>
      {problems.length > 0 && <ErrorNote>{problems.join(" ")}</ErrorNote>}
      {review?.stamp === stamp && (
        <>
          <SourceLinkPreview links={review.links} expectedKind={source.kind} />
          {reviewed && (
            <p role="status" className="text-sm">
              {links.length} links to add · {selected.length} items to remove. Link format reviewed. These changes have
              not been saved.
            </p>
          )}
        </>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}
