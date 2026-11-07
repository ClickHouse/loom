import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Table } from "@clickhouse/click-ui/Table";
import { TextField } from "@clickhouse/click-ui/TextField";
import { getOwnerReport, type OwnerReport } from "../../api";
import { useIdentity } from "../../lib/state";
import { humanizeError } from "../../lib/format";
import { AgentBadge } from "../../components/AgentBadge";
import { ListTime } from "../../components/ui/ListTime";
import { Alert, SectionHeading } from "../../components/ui";

/** One user's memory in this namespace (memory.owner_report): how many rows they own,
 *  which tokens and agents read them in the last 30 days, and how often they were used. */
export function OwnerReportSection() {
  const identity = useIdentity();
  const [userId, setUserId] = useState("");
  const [report, setReport] = useState<OwnerReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const read = async () => {
    setBusy(true);
    setError(null);
    try {
      setReport(await getOwnerReport(identity, userId.trim()));
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="mt-8">
      <SectionHeading label="One user's memory" level={2} caption="Who read what one user's memories hold." />
      <form
        className="mt-2 flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void read();
        }}
      >
        <TextField id="owner-user-id" label="User id" value={userId} onChange={setUserId} />
        <Button type="secondary" htmlType="submit" disabled={busy || !userId.trim()} loading={busy}>
          Show
        </Button>
      </form>
      {error && <Alert role="alert" state="danger" text={error} />}
      {report && (
        <div className="mt-3 space-y-2">
          <p className="text-sm">
            {report.user_id} owns {report.live_rows.toLocaleString()} memories here.
          </p>
          {report.reads.length === 0 ? (
            <p className="text-sm text-text-muted">Nothing read them in the last 30 days.</p>
          ) : (
            <Table
              aria-label={`Reads of ${report.user_id}'s memories`}
              size="sm"
              headers={[
                { label: "Token" },
                { label: "Agent" },
                { label: "Memories read", width: "130px" },
                { label: "Reads", width: "90px" },
                { label: "Last read", width: "120px" },
              ]}
              rows={report.reads.map((r) => ({
                id: `${r.actor}/${r.agent}`,
                items: [
                  { label: <span className="font-mono text-xs">{r.actor}</span> },
                  { label: <AgentBadge agent={r.agent} /> },
                  { label: <span className="tabular">{r.distinct_keys.toLocaleString()}</span> },
                  { label: <span className="tabular">{r.calls.toLocaleString()}</span> },
                  { label: <ListTime time={r.last_read} quiet /> },
                ],
              }))}
            />
          )}
        </div>
      )}
    </section>
  );
}
