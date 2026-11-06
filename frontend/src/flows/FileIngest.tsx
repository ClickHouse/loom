/**
 * The pieces both add dialogs show for a file: what the deployment will read it as
 * (`knowledge.formats`), and where its ingest job is. The browser parses nothing:
 * the server's extractor does, and the document shows its text once it publishes.
 */
import { useEffect, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
import { type Identity, type KnowledgeFormats, knowledgeFormats } from "../api";
import { formatBytesShort } from "../lib/format";
import type { FileVerdict } from "../lib/fileIngest";

/** The deployment's formats, read when `open` turns true; null until read, or from a
 *  server that predates `knowledge.formats`. */
export function useKnowledgeFormats(id: Pick<Identity, "org" | "namespace">, open: boolean): KnowledgeFormats | null {
  const [formats, setFormats] = useState<KnowledgeFormats | null>(null);
  const { org, namespace } = id;
  useEffect(() => {
    if (!open) return;
    const ctrl = new AbortController();
    knowledgeFormats({ org, namespace }, { signal: ctrl.signal })
      .then(setFormats)
      .catch(() => setFormats(null)); // unread: the dialog still sends the file and reports the job
    return () => ctrl.abort();
  }, [open, org, namespace]);
  return formats;
}

/** The formats a person can pick, in words: "PDF, Markdown, …" (the available ones). */
export function readableFormats(formats: KnowledgeFormats | null): string {
  if (!formats) return "";
  return formats.formats
    .filter((f) => f.available)
    .map((f) => f.format)
    .join(", ");
}

/** A picked file: its name, size and the format it is read as, with whether this
 *  deployment reads it. */
export function PickedFile({ file, verdict }: { file: File; verdict: FileVerdict }) {
  return (
    <div className="flex flex-wrap items-center justify-center gap-2">
      <Icon name="document" size="sm" aria-hidden="true" />
      <span className="font-medium break-all">{file.name}</span>
      <span className="text-text-muted">· {formatBytesShort(file.size)}</span>
      {verdict.format && (
        <Badge
          size="sm"
          state={verdict.available === false ? "danger" : verdict.available ? "success" : "neutral"}
          text={
            verdict.available === false
              ? `${verdict.format}: not read here`
              : verdict.available === null
                ? `${verdict.format}: extractor not answering`
                : verdict.format
          }
        />
      )}
    </div>
  );
}

/** "Publish what could be read" for a file part of which may not extract. */
export function AllowPartial({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <Checkbox
      label="Publish what could be read if part of the file cannot be (marked partial)"
      checked={checked}
      onCheckedChange={(c) => onChange(c === true)}
    />
  );
}

/** Where the upload and its job are: one line, read out as it changes. */
export function IngestProgress({ step }: { step: string }) {
  if (!step) return null;
  return (
    <p role="status" aria-live="polite" className="text-sm text-text-muted">
      {step}
    </p>
  );
}
