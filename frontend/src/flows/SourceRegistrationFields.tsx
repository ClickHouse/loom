import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import {
  credentialLabel,
  INCLUDE_OPTIONS,
  KIND_OPTIONS,
  toggleInclude,
  type SourceForm,
} from "../lib/knowledgeSources";
import { SourceAccountField, type useSourceAccount } from "./SourceAccountField";

export type SetField = <K extends keyof SourceForm>(key: K, value: SourceForm[K]) => void;

function SourceIncludes({ form, set }: { form: SourceForm; set: SetField }) {
  return (
    <fieldset>
      <legend className="mb-1.5 block text-xs font-medium text-text-muted">File kinds to pull</legend>
      <div className="flex flex-wrap gap-4">
        {INCLUDE_OPTIONS.map((o) => (
          <Checkbox
            key={o.value}
            label={o.label}
            checked={form.include.includes(o.value)}
            onCheckedChange={() => set("include", toggleInclude(form.include, o.value))}
          />
        ))}
      </div>
    </fieldset>
  );
}

/** The chosen kind's ids and filters, as the server's config for that kind takes them. */
function SourceFields({ form, set }: { form: SourceForm; set: SetField }) {
  switch (form.kind) {
    case "gdrive":
      return (
        <>
          <TextAreaField
            id="ksrc-folders"
            label="Folders (ids or folder links, one per line)"
            rows={3}
            className="font-mono"
            value={form.folders}
            onChange={(v) => set("folders", v)}
          />
          <SourceIncludes form={form} set={set} />
        </>
      );
    case "linear":
      return (
        <TextAreaField
          id="ksrc-projects"
          label="Projects (project ids, UUIDs, one per line)"
          rows={3}
          className="font-mono"
          value={form.projects}
          onChange={(v) => set("projects", v)}
        />
      );
    case "notion":
      return (
        <>
          <TextAreaField
            id="ksrc-pages"
            label="Pages (ids or page links, one per line)"
            rows={2}
            className="font-mono"
            value={form.pages}
            onChange={(v) => set("pages", v)}
          />
          <TextAreaField
            id="ksrc-databases"
            label="Databases (ids or database links, one per line; 20 pages and databases in all)"
            rows={2}
            className="font-mono"
            value={form.databases}
            onChange={(v) => set("databases", v)}
          />
        </>
      );
    case "recording":
      return (
        <TextAreaField
          id="ksrc-meetings"
          label="Meetings (meeting ids, one per line)"
          rows={3}
          className="font-mono"
          value={form.meetings}
          onChange={(v) => set("meetings", v)}
        />
      );
  }
}

export function SourceRegistrationFields({
  form,
  set,
  label,
  account,
  busy,
  allowShared,
}: {
  form: SourceForm;
  set: SetField;
  label: string;
  account: ReturnType<typeof useSourceAccount>;
  busy: boolean;
  allowShared: boolean;
}) {
  const connected = form.credentialMode === "connected";
  return (
    <>
      <p className="text-sm">
        Destination: <strong>{label}</strong>
      </p>
      <p className="text-xs text-text-muted">
        To change the destination, close this dialog and choose a place in the Library.
      </p>
      <fieldset disabled={busy} aria-label="Source details" className="space-y-4">
        <TextField
          label="Name (2-32 lower-case letters, digits, - or _, starting with a letter)"
          value={form.name}
          autoComplete="off"
          placeholder="design-docs"
          onChange={(v) => set("name", v)}
          autoFocus
        />
        {/* Locked while a call runs, so its answer lands on the form it was sent for. */}
        <ChoiceGroup
          label="Kind"
          inline
          disabled={busy}
          value={form.kind}
          onChange={(v) => set("kind", v)}
          options={KIND_OPTIONS.filter((option) => allowShared || option.value !== "recording")}
        />
        {form.kind !== "recording" && (
          <ChoiceGroup
            label="Account"
            value={form.credentialMode}
            onChange={(value) => set("credentialMode", value)}
            options={[
              { value: "connected", label: "My connected account" },
              ...(allowShared ? [{ value: "shared" as const, label: "Shared credential" }] : []),
            ]}
          />
        )}
        {connected && form.kind !== "recording" ? (
          <SourceAccountField kind={form.kind} account={account} busy={busy} />
        ) : (
          <PasswordField
            label={credentialLabel(form.kind)}
            value={form.credential}
            autoComplete="new-password"
            onChange={(v) => set("credential", v)}
          />
        )}
        <p className="text-xs text-text-muted">
          {connected
            ? "Only readers whose access Loom can verify with the provider can read connected content. If access cannot be verified, it stays hidden."
            : "Everyone who can read this destination can read content imported with this shared credential. Provider permissions are not mirrored."}
        </p>
        {form.kind !== "recording" && (
          <ChoiceGroup
            label="Select content"
            value={form.inputMode}
            onChange={(value) => set("inputMode", value)}
            options={[
              { value: "links", label: "Paste links" },
              { value: "ids", label: "Enter IDs" },
            ]}
          />
        )}
        {form.inputMode === "links" && form.kind !== "recording" ? (
          <>
            <TextAreaField
              id="ksrc-links"
              label="Content links (one per line)"
              rows={4}
              value={form.links}
              onChange={(value) => set("links", value)}
              aria-describedby="ksrc-link-help"
            />
            <p id="ksrc-link-help" className="text-xs text-text-muted">
              {form.kind === "gdrive"
                ? "Paste Drive folders, files, Docs or Slides links. Up to 300 links."
                : form.kind === "notion"
                  ? "Paste up to 20 Notion page or database links."
                  : "Paste up to 20 Linear project links. This imports project documents; issues and comments are not imported."}
            </p>
            {form.kind === "gdrive" && <SourceIncludes form={form} set={set} />}
          </>
        ) : (
          <SourceFields form={form} set={set} />
        )}
      </fieldset>
    </>
  );
}
