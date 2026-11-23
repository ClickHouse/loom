import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { useRef, useState } from "react";
import type { CollectionEntry } from "../api";
import {
  type MCPAudience, type MCPMode, type MCPPreview, type MCPSelection, type MCPSource,
  mcpAdd, mcpInspect, mcpInspectConnection, mcpPreview, mcpUpdate,
} from "../api/mcpSources";
import { Alert } from "../components/ui/Alert";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { Loading } from "../components/ui/Loading";
import { humanizeError } from "../lib/errorWords";
import type { DashboardRequestIntent } from "../lib/dashboardTransport";
import { MCPConnectionStep, type MCPChosenConnection } from "./MCPConnectionStep";
import { mcpDestinationCopy, mcpSourceCopy as copy } from "./mcpSourceCopy";
import { SourceDestinationStep } from "./SourceDestinationStep";
import { DialogFrame } from "./TaskDialogFrame";
import { useMCPRequest } from "./useMCPRequest";
import { MCPResourceBrowser } from "./MCPResourceBrowser";

export interface MCPSourceSetupProps {
  org: string;
  callerKey: string;
  collectionVersion: string;
  source?: MCPSource;
  onSaved: (source: MCPSource) => void;
  onClose: () => void;
}

/** A source nonce and confirmed collection identity remain in memory across uncertain saves. */
export function MCPSourceSetup(props: MCPSourceSetupProps) {
  return <SourceDraft key={`${props.org}/${props.callerKey}`} {...props} />;
}

function SourceDraft({ org, callerKey, collectionVersion, source, onSaved, onClose }: MCPSourceSetupProps) {
  const [chosen, setChosen] = useState<MCPChosenConnection | null>(null);
  const [connectionUncertain, setConnectionUncertain] = useState(false);
  const [resources, setResources] = useState(source?.resource_ids.join("\n") ?? "");
  const [mode, setMode] = useState<MCPMode>(source?.mode ?? "live");
  const [audience, setAudience] = useState<MCPAudience>(source?.reader_policy ?? "owner_only");
  const [label, setLabel] = useState(source?.label ?? "");
  const [destination, setDestination] = useState<CollectionEntry | null>(null);
  const [created, setCreated] = useState<string[]>([]);
  const [preview, setPreview] = useState<{ key: string; value: MCPPreview } | null>(null);
  const [consent, setConsent] = useState(false);
  const [invalidation, setInvalidation] = useState(0);
  const [requestId] = useState(() => crypto.randomUUID());
  const [uncertain, setUncertain] = useState(false);
  const [binding, setBinding] = useState<{ org: string; callerKey: string; namespace: string; slot: string; request_id: string } | null>(null);
  const [inspection, setInspected] = useState<{ key: string; source: MCPSource } | null>(null);
  const [saved, setSaved] = useState<MCPSource | null>(null);
  const [notice, setNotice] = useState("");
  const ids = resourceIDs(resources);
  const namespace = source?.namespace ?? destination?.namespace ?? "";
  const slot = source?.slot ?? chosen?.connection.slot ?? "";
  const contentContext = JSON.stringify([slot, chosen?.connection.generation, resources, mode, audience]);
  const key = JSON.stringify([org, callerKey, collectionVersion, contentContext, namespace, label, invalidation]);
  const currentKey = useRef(key);
  currentKey.current = key;
  const request = useMCPRequest(key);
  const invalidate = () => { setPreview(null); setConsent(false); setInvalidation((v) => v + 1); };
  const result = preview?.key === key ? preview.value : null;
  const inspected = inspection?.key === key ? inspection.source : null;
  const selection: MCPSelection = { org, namespace, slot, resource_ids: ids, mode, reader_policy: audience };
  const valid = !!slot && !!namespace && ids.length > 0 && ids.length <= 1000 && new Set(ids).size === ids.length &&
    ids.every((id) => !/[\r\u0000]/.test(id)) && label.length <= 256;
  const canSave = valid && !!result && result.rule.access === (audience === "collection_copy" ? "collection_wide" : "owner_only") && result.connection_state === "configured" &&
    (!result.rule.publication_confirmation_required || consent) && (audience !== "collection_copy" || consent);
  const locked = request.busy || uncertain || saved !== null;

  const check = async () => {
    if (!valid || locked) return;
    setPreview(null); setConsent(false);
    const stamp = key;
    const answer = await request.run(async (signal) => {
      const connection = await mcpInspectConnection(org, { slot }, signal);
      if (!connection.found || !connection.connection || connection.connection.slot !== slot || connection.connection.state !== "configured" ||
        (chosen && connection.connection.generation !== chosen.connection.generation))
        throw new Error("Connection changed. Select and discover it again.");
      const value = await mcpPreview(selection, signal);
      if (value.namespace !== namespace || value.mode !== mode || value.selected_count !== ids.length)
        throw new Error("The preview does not match this draft. Review access again.");
      return value;
    });
    if (answer && currentKey.current === stamp) setPreview({ key: stamp, value: answer });
  };
  const save = async (intent: DashboardRequestIntent) => {
    if (!canSave || locked) return;
    // Mark before sending; a canceled or unreadable answer must never invite another mutation.
    setUncertain(true); setInspected(null);
    setBinding({ org, callerKey, namespace, slot, request_id: requestId });
    const answer = await request.run(async (signal) => {
      if (source) {
        const latest = await mcpInspect({ org, namespace, name: source.name }, signal);
        if (!latest.found || !latest.source || latest.source.name !== source.name || latest.source.namespace !== namespace ||
          latest.source.slot !== source.slot || latest.source.reader_policy !== source.reader_policy || latest.source.scope_revision !== source.scope_revision)
          throw new Error("The source revision changed. Inspect its current state before editing.");
        if (source.scope_revision === null) throw new Error("This source has no editable scope revision.");
        return mcpUpdate({ org, namespace, name: source.name, if_scope_revision: source.scope_revision,
          resource_ids: ids, mode, label: label || null, confirm_copy_authority: consent }, signal, intent);
      }
      return mcpAdd({ ...selection, request_id: requestId, label: label || null, confirm_copy_authority: consent }, signal, intent);
    });
    if (answer) { setUncertain(false); setSaved(answer.source); onSaved(answer.source); }
  };
  const reconcile = async () => {
    if (!binding || binding.org !== org || binding.callerKey !== callerKey || request.busy) return;
    setInspected(null);
    const answer = await request.run((signal) => mcpInspect(source ?
      { org: binding.org, namespace: binding.namespace, name: source.name } :
      { org: binding.org, namespace: binding.namespace, slot: binding.slot, request_id: binding.request_id }, signal));
    if (!answer) return;
    const matches = answer.found && answer.source && answer.source.namespace === binding.namespace && answer.source.slot === binding.slot &&
      (!source || answer.source.name === source.name);
    setInspected(matches && answer.source ? { key, source: answer.source } : null);
    setNotice(matches ? (source ? "Current source state returned. This does not attest completion of the original update." : "The server reconciled the original request and destination. Review its current registration before accepting it.") : "No matching owned registration was returned. The original result remains uncertain; do not resubmit.");
  };
  return (
    <DialogFrame open onOpenChange={(open) => { if (!open && !request.busy && !uncertain && !connectionUncertain) onClose(); }} width={760}
      title={source ? "Edit MCP source selection" : "Set up an MCP source"}
      description="Choose an account, a fixed selection and a collection, then review server access rules."
      footer={<>
        <Button type="secondary" htmlType="button" disabled={request.busy} onClick={onClose}>{uncertain || connectionUncertain ? "Close and discard this in-memory draft" : "Close"}</Button>
        {saved ? <Button type="primary" htmlType="button" onClick={onClose}>Done</Button> :
          <Button type="primary" htmlType="button" disabled={locked || !canSave} onClick={() => void save("explicit")}>{source ? "Save source changes" : "Register source"}</Button>}
      </>}>
      <p className="text-xs text-text-muted">{copy.privacy}</p>
      {!source && <MCPConnectionStep org={org} callerKey={callerKey} contextKey={collectionVersion} disabled={locked} onUncertainChange={setConnectionUncertain}
        onChange={(value) => { invalidate(); setChosen(value); }} />}
      {(source || chosen) && <>
        {!source && chosen && <MCPResourceBrowser key={`${slot}/${chosen.connection.generation}`} org={org} slot={slot} generation={chosen.connection.generation}
          contextKey={collectionVersion} disabled={locked} selected={ids} onSelect={(id) => { invalidate(); setResources((value) => value ? `${value}\n${id}` : id); }} />}
        {source && <p className="text-sm">Registration: {source.name}. Destination: {source.namespace}. Audience: {source.reader_policy}. Audience and connection are fixed; changing either requires a new registration.</p>}
        <fieldset disabled={locked} className="space-y-3">
          <TextAreaField label="Explicit resource IDs (one per line)" value={resources} rows={5} onChange={(value) => { invalidate(); setResources(value); }} aria-describedby="mcp-selection-hint" />
          <p id="mcp-selection-hint" className="text-xs text-text-muted">{copy.selection} Select 1–1,000 distinct IDs without empty lines.</p>
          <TextField label="Source label (optional)" value={label} onChange={(value) => { invalidate(); setLabel(value); }} />
          <ChoiceGroup label="Source mode" value={mode} options={copy.modes} onChange={(value) => { invalidate(); setMode(value); }} />
          {!source && <ChoiceGroup label="Publication audience" value={audience} onChange={(value) => { invalidate(); setAudience(value); }} options={[
            { value: "owner_only", label: "Private to me", hint: "Collection membership does not broaden this source's audience." },
            { value: "collection_copy", label: "Copy to collection readers", hint: "Requires operator approval, current collection admin and explicit publication authority.", disabled: mode === "live" },
          ]} />}
        </fieldset>
        <p className="text-sm">{copy.audience}</p>
        {!source && <SourceDestinationStep org={org} callerKey={callerKey} contextKey={contentContext} collectionVersion={collectionVersion}
          value={destination} onChange={setDestination} onInvalidated={invalidate} copy={mcpDestinationCopy} disabled={locked}
          onCollectionCreated={(value) => { if (value.org === org && value.callerKey === callerKey) setCreated((old) => [...new Set([...old, value.namespace])]); }} />}
        <Button type="secondary" htmlType="button" disabled={!valid || locked} onClick={() => void check()}>Review effective access</Button>
        {result && <MCPAccessPreview preview={result} />}
        {result && (result.rule.publication_confirmation_required || audience === "collection_copy") && <Checkbox label={copy.authority} checked={consent} disabled={locked || result.rule.access === "unavailable"} onCheckedChange={(value) => setConsent(value === true)} />}
      </>}
      {created.length > 0 && <p role="status" className="text-sm">Confirmed collections retained: {created.join(", ")}. A failed source registration does not remove them.</p>}
      {request.busy && <Loading label="Waiting for the source response" />}
      {!!request.error && <Alert role="alert" state="danger" text={humanizeError(request.error)} />}
      {uncertain && <div className="space-y-2">
        <Alert state="warning" text={copy.uncertain} />
        <p className="text-sm">Original destination: {binding?.namespace}. Original slot: {binding?.slot}. Retained request ID: {binding?.request_id}.</p>
        <Button type="secondary" htmlType="button" disabled={!binding || request.busy} onClick={() => void reconcile()}>Inspect source result</Button>
        {inspected && <>
          <p className="text-sm">{inspected.name}: {inspected.state}; {inspected.mode}; {inspected.reader_policy}; revision {inspected.scope_revision}. IDs: {inspected.resource_ids.join("; ")}</p>
          <Button type="secondary" htmlType="button" onClick={() => { if (binding?.org === org && binding.callerKey === callerKey) { setSaved(inspected); setUncertain(false); onSaved(inspected); } }}>Accept inspected server state</Button>
        </>}
      </div>}
      {notice && <p role="status">{notice}</p>}
      {saved && <>
        <p className="text-sm">Registration name: {saved.name}. Destination: {saved.namespace}.</p>
        <p role="status">Server registration state: {saved.state}. Refresh state: {saved.refresh_state}. This does not attest upstream access or completed synchronization.</p>
      </>}
    </DialogFrame>
  );
}

/** Newline is the sole delimiter; neither URLs nor opaque identities are normalized. */
export function resourceIDs(value: string): string[] { return value === "" ? [] : value.split("\n"); }

export function MCPAccessPreview({ preview }: { preview: MCPPreview }) {
  return <section aria-label="Effective access preview" className="space-y-2 text-sm">
    <p>Server access rule: {preview.rule.access}. Reason: {preview.rule.reason || "No additional reason returned"}.</p>
    <p>{preview.rule.explanation}</p>
    <p>Stores a copy: {preview.rule.stores_copy ? "yes" : "no"}. Refresh: {preview.refresh_state}. Selected IDs: {preview.selected_count}.</p>
    <p>Checked at: {preview.checked_at}. Preview grants authority: no. Remote permission evidence: {preview.permission_evidence}. Reader list complete: no.</p>
    <p>Read evidence expires after {preview.read_evidence_stale_seconds} seconds. Snapshot read access expires without manual refresh; retained bytes do not grant permanent access. Both audiences require current authorization and fresh evidence.</p>
    <ul className="list-disc pl-5" aria-label="Access limitations">{preview.limitations.map((line, index) => <li key={`${index}:${line}`}>{line}</li>)}</ul>
  </section>;
}
