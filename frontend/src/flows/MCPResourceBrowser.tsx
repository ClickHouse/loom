import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import { useEffect, useState } from "react";
import { type MCPBrowse, mcpBrowse } from "../api/mcpSources";
import { Alert } from "../components/ui/Alert";
import { BusyButton } from "../components/ui/BusyButton";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { Loading } from "../components/ui/Loading";
import { useMCPRequest } from "./useMCPRequest";

export function MCPResourceBrowser({ org, slot, generation, contextKey, disabled, selected, onSelect }: {
  org: string; slot: string; generation: string; contextKey: string; disabled: boolean;
  selected: string[]; onSelect: (id: string) => void;
}) {
  const [mode, setMode] = useState<"resources" | "search">("resources");
  const [query, setQuery] = useState("");
  const [window, setWindow] = useState<{ key: string; value: MCPBrowse } | null>(null);
  const authority = JSON.stringify([org, slot, generation, contextKey]);
  useEffect(() => { setWindow(null); setQuery(""); }, [authority]);
  const scope = JSON.stringify([org, slot, generation, contextKey, mode, query]);
  const request = useMCPRequest(scope);
  const result = window?.key === scope ? window.value : null;
  const valid = mode === "resources" || (query.trim().length > 0 && query.length <= 1000 && new TextEncoder().encode(query).length <= 4096);
  const browse = async () => {
    if (disabled || request.busy || !valid) return;
    setWindow(null);
    const answer = await request.run(async (signal) => {
      const value = await mcpBrowse({ org, slot, mode, query, limit: 100 }, signal);
      if (value.slot !== slot || value.generation !== generation || value.mode !== mode)
        throw new Error("Connection changed. Discover capabilities again.");
      return value;
    });
    if (answer) setWindow({ key: scope, value: answer });
  };
  return <section aria-label="Browse MCP resource metadata" className="space-y-2">
    <p className="text-sm">Optional metadata window. Choose standard resource listing or an operator-mapped search. Capabilities come from the approved profile and server discovery. Enter manual resource IDs below at any time.</p>
    <ChoiceGroup label="Metadata operation" value={mode} disabled={disabled} onChange={setMode} options={[
      { value: "resources", label: "List resources" }, { value: "search", label: "Mapped search" },
    ]} />
    {mode === "search" && <TextField label="Mapped search query" value={query} onChange={setQuery} disabled={disabled} />}
    <BusyButton type="secondary" htmlType="button" busy={request.busy} disabled={disabled || !valid} onClick={() => void browse()}>Read first metadata window</BusyButton>
    {request.busy && <Loading label="Reading resource metadata" />}
    {!!request.error && <Alert role="alert" state="danger" text="Resource metadata could not be read. No content was selected or imported. Protected transport refusals remain failures." />}
    {result && <div className="space-y-2 text-sm">
      <p>Supported: {result.supported ? "yes" : "no"}. Capabilities checked: {result.capabilities_checked ? "yes" : "no"}. Resources: {result.resources === null ? "unchecked" : result.resources ? "yes" : "no"}. Approved read mapping: {result.mapped_read ? "yes" : "no"}. Approved search mapping: {result.mapped_search ? "yes" : "no"}.</p>
      <p>First window only. Declared page fits this window: {result.complete ? "yes" : "no"}. Truncated: {result.truncated ? "yes" : "no"}. Reason: {result.reason ?? "none returned"}. This does not prove a complete tree, corpus, deletion or ACL access. No continuation is available. Manual IDs remain possible.</p>
      <ul aria-label="Resource metadata" className="space-y-2">{result.items.map((item) => <li key={item.id} className="break-words">
        <p>{item.title}</p><p>{item.id}</p><p>Display hint only: {item.uri}</p>
        <Button type="secondary" htmlType="button" disabled={disabled || !result.supported || selected.includes(item.id) || selected.length >= 1000 || !item.id || /[\r\n\u0000]/.test(item.id)} onClick={() => onSelect(item.id)}>Select ID {item.id}</Button>
      </li>)}</ul>
      <ul aria-label="Browse limitations" className="list-disc pl-5">{result.limitations.map((line, index) => <li key={`${index}:${line}`}>{line}</li>)}</ul>
      <Button type="secondary" htmlType="button" onClick={() => { setWindow(null); setQuery(""); }}>Clear metadata</Button>
    </div>}
  </section>;
}
