import { type CSSProperties, useEffect, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { moveKnowledge, type KnowledgeMoveResult, type MoveRefusal } from "../api";
import { toast } from "../components/Toast";
import { Alert } from "../components/ui";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { humanizeError } from "../lib/errorWords";
import { fmtInt } from "../lib/format";

/** A place documents can go to: a collection the person writes, their own files, or the
 *  header's namespace. */
export interface MovePlace {
  namespace: string;
  label: string;
  /** Its depth in the collection tree, for the indent. */
  depth: number;
  kind: "collection" | "personal" | "namespace";
}

// Why a document stayed, by `knowledge.move`'s reason.
const STAYED: Record<MoveRefusal, string> = {
  not_found: "no longer here",
  exists: "a document with the same source is already there",
  synced: "kept here by a document source",
  busy: "busy, or cut off partway; moving it again finishes it",
  erased: "an erasure is under way",
};

function documents(n: number): string {
  return `${fmtInt(n)} document${n === 1 ? "" : "s"}`;
}

/** The toast after a move: how many went, and why the rest stayed. */
export function moveSummary(results: KnowledgeMoveResult[], to: string): { text: string; ok: boolean } {
  const moved = results.filter((r) => r.moved).length;
  const stayed = new Map<MoveRefusal, number>();
  for (const r of results) if (!r.moved && r.reason) stayed.set(r.reason, (stayed.get(r.reason) ?? 0) + 1);
  const why = [...stayed].map(([reason, n]) => `${documents(n)} stayed (${STAYED[reason]})`).join("; ");
  if (!why) return { text: `Moved ${documents(moved)} to ${to}`, ok: true };
  return { text: moved ? `Moved ${documents(moved)} to ${to}; ${why}` : `Nothing moved: ${why}`, ok: false };
}

/** Who reads the documents once they are in `place`. */
function sharing(place: MovePlace): string {
  if (place.kind === "personal") return "Only you read them there.";
  if (place.kind === "namespace") return `Whoever reads the namespace ${place.namespace} reads them there.`;
  return `They take ${place.label}'s sharing: whoever reads it reads them.`;
}

/** Move documents to another place (`knowledge.move`): each is written there whole and
 *  deleted here. */
export function MoveDocumentsDialog({
  org,
  from,
  fromLabel,
  sourceUris,
  places,
  placesReading = null,
  onMoved,
  onClose,
  onCloseAutoFocus,
}: {
  org: string;
  /** The namespace the documents are in, and what it is called on screen. */
  from: string;
  fromLabel: string;
  sourceUris: string[];
  /** Where they can go, in tree order; `from` is left out by the caller. */
  places: MovePlace[];
  /** The collections are still being read, or their read failed: `places` may lack them. */
  placesReading?: "loading" | "failed" | null;
  /** After every call, whatever it answered (a failed one may have moved some): the
   *  sources that moved. */
  onMoved: (moved: string[]) => void;
  onClose: () => void;
  /** Where focus goes once it closes, as the Modal's. */
  onCloseAutoFocus?: (e: Event) => void;
}) {
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // What is still to move: a second Move after a stop sends only these.
  const [left, setLeft] = useState(sourceUris);
  // Closing it stops the calls still to send; the one in flight runs to its answer.
  const stop = useRef<AbortController | null>(null);
  useEffect(() => () => stop.current?.abort(), []);
  const target = places.find((p) => p.namespace === to) ?? null;
  const what = documents(left.length);

  const submit = async () => {
    if (!target) return;
    setBusy(true);
    setError(null);
    const controller = new AbortController();
    stop.current = controller;
    const r = await moveKnowledge({ org, namespace: from }, target.namespace, left, controller.signal);
    const moved = r.results.filter((x) => x.moved).map((x) => x.source_uri);
    onMoved(moved);
    if (controller.signal.aborted) return; // closed meanwhile: the list is read again
    if (r.found && r.error === undefined) {
      const summary = moveSummary(r.results, target.label);
      toast(summary.text, summary.ok ? "ok" : "info");
      onClose();
      return;
    }
    const gone = new Set(moved);
    setLeft(left.filter((u) => !gone.has(u)));
    // A selection sent in several calls may have moved some before the stop.
    const before = moved.length ? ` ${documents(moved.length)} moved before that.` : "";
    if (!r.found) {
      setError(`You can no longer add documents to ${target.label}.${before}`);
    } else {
      // The failed call may have moved some of its own before it failed.
      setError(
        `${humanizeError(r.error)}${before} Some documents of the last call may have moved too; the list shows where each one is now.`,
      );
    }
    setBusy(false);
  };

  return (
    <Modal
      open
      onOpenChange={(v) => !v && onClose()}
      onCloseAutoFocus={onCloseAutoFocus}
      title={`Move ${what}`}
      className="w-full max-w-[480px]"
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-4">
          <p className="text-sm text-text-muted">{`From ${fromLabel}`}</p>
          <Select id="move-documents-to" label="To" value={to} placeholder="Pick a place" onSelect={setTo} showSearch>
            {places.map((p) => (
              <Select.Item key={p.namespace} value={p.namespace}>
                <span className="collection-depth" style={{ "--depth": p.depth } as CSSProperties}>
                  {p.label}
                </span>
              </Select.Item>
            ))}
          </Select>
          {target && <p className="text-sm text-text-muted">{sharing(target)}</p>}
          {placesReading === "loading" && <p className="text-sm text-text-muted">Reading the collections…</p>}
          {placesReading === "failed" && (
            <p className="text-sm text-text-muted">The collections could not be read, so they are not listed here.</p>
          )}
          {places.length === 0 && !placesReading && (
            <p className="text-sm text-text-muted">There is no other place you can add documents to.</p>
          )}
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={busy || !target} loading={busy}>
            Move
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
