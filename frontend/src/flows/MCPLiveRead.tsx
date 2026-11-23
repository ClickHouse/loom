import { Button } from "@clickhouse/click-ui/Button";
import { useEffect, useState } from "react";
import { type MCPLiveRead as LiveRead, type MCPSource, mcpRead } from "../api/mcpSources";
import { Alert } from "../components/ui/Alert";
import { BusyButton } from "../components/ui/BusyButton";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { Loading } from "../components/ui/Loading";
import { useMCPRequest } from "./useMCPRequest";

export function MCPLiveRead({ org, source, contextKey, disabled }: {
  org: string; source: MCPSource; contextKey: string; disabled: boolean;
}) {
  const [id, setId] = useState("");
  const [epoch, setEpoch] = useState(0);
  const [answer, setAnswer] = useState<{ key: string; value: LiveRead } | null>(null);
  const authority = JSON.stringify([org, source, contextKey, disabled]);
  useEffect(() => { setAnswer(null); }, [authority]);
  const scope = JSON.stringify([org, source, contextKey, id, epoch, disabled]);
  const request = useMCPRequest(scope);
  const result = answer?.key === scope ? answer.value : null;
  const read = async () => {
    if (disabled || request.busy || !source.resource_ids.includes(id) || source.mode !== "live" || source.state !== "registered") return;
    setAnswer(null);
    const value = await request.run(async (signal) => {
      const out = await mcpRead({ org, namespace: source.namespace, name: source.name, resource_id: id }, signal);
      if (out.source_id !== source.source_id || out.resource_id !== id) throw new Error("Read identity mismatch.");
      return out;
    });
    if (value) setAnswer({ key: scope, value });
  };
  return <section aria-label="Read a saved live resource" className="space-y-2">
    <p className="text-sm">Explicit owner live read of one saved ID. This reads the bounded text without importing it. Current server authorization is required; remote identity and upstream ACLs remain unverified. Closing or clearing this view discards the displayed body.</p>
    <ChoiceGroup label="Saved resource to read" value={id} onChange={(value) => { setAnswer(null); setId(value); }} disabled={disabled} options={source.resource_ids.map((value) => ({ value, label: value }))} />
    <BusyButton type="secondary" htmlType="button" busy={request.busy} disabled={disabled || !source.resource_ids.includes(id)} onClick={() => void read()}>Read selected live resource</BusyButton>
    <Button type="secondary" htmlType="button" onClick={() => { setAnswer(null); setId(""); setEpoch((v) => v + 1); }}>Clear live content</Button>
    {request.busy && <Loading label="Reading the saved live resource" />}
    {!!request.error && <Alert role="alert" state="danger" text="The live resource could not be read. Protected transport or authorization refusals remain failures. No body was imported." />}
    {result && <div className="space-y-2 text-sm">
      <p>{result.title}</p><p>Stable citation: {result.uri}</p><p>MIME: {result.mime_type}. Resource identity verified: {result.resource_identity_verified ? "yes" : "no"}. Remote identity verified: no. Upstream ACL verified: no.</p>
      <pre className="whitespace-pre-wrap break-words" aria-label="Live resource text">{result.text}</pre>
      <ul aria-label="Live read limitations" className="list-disc pl-5">{result.limitations.map((line, index) => <li key={`${index}:${line}`}>{line}</li>)}</ul>
    </div>}
  </section>;
}
