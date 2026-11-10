import { useEffect, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Table } from "@clickhouse/click-ui/Table";
import { TextField } from "@clickhouse/click-ui/TextField";
import { OWNER_READS, OWNER_REPORT_HOURS, getOwnerReport, type OwnerReport } from "../../api";
import { useIdentity } from "../../lib/state";
import { humanizeError } from "../../lib/errorWords";
import { AgentBadge, NameBadge } from "../../components/AgentBadge";
import { ListTime } from "../../components/ui/ListTime";
import { Alert, SectionHeading } from "../../components/ui";

const WINDOW = `the last ${OWNER_REPORT_HOURS / 24} days`;

/** One user's memory in this namespace (memory.owner_report): how many rows they own and
 *  which readers and agents read them over `OWNER_REPORT_HOURS`, the most frequent first. */
export function OwnerReportSection() {
  const identity = useIdentity();
  const [userId, setUserId] = useState("");
  const [report, setReport] = useState<OwnerReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The read out, aborted by a newer one or by leaving the namespace.
  const reading = useRef<AbortController | null>(null);
  useEffect(() => {
    setReport(null);
    setError(null);
    setBusy(false);
    return () => reading.current?.abort();
  }, [identity.org, identity.namespace]);
  const read = async () => {
    reading.current?.abort();
    const ctl = new AbortController();
    reading.current = ctl;
    setBusy(true);
    setError(null);
    try {
      const r = await getOwnerReport(identity, userId.trim(), { signal: ctl.signal });
      if (!ctl.signal.aborted) setReport(r);
    } catch (e) {
      if (!ctl.signal.aborted) setError(humanizeError(e));
    } finally {
      if (!ctl.signal.aborted) setBusy(false);
    }
  };
  return (
    <section className="mt-8">
      <SectionHeading
        label="One user's memory"
        level={2}
        caption={`Which readers and agents read one user's memories in ${WINDOW}.`}
      />
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
            {report.user_id} owns {report.live_rows.toLocaleString()} memor{report.live_rows === 1 ? "y" : "ies"} here.
            {report.reads.length >= OWNER_READS && ` The ${OWNER_READS} most frequent readers:`}
          </p>
          {report.reads.length === 0 ? (
            <p className="text-sm text-text-muted">Nothing read them in {WINDOW}.</p>
          ) : (
            <Table
              aria-label={`Reads of ${report.user_id}'s memories`}
              size="sm"
              headers={[
                { label: "Reader" },
                { label: "Agent" },
                { label: "Memories read", width: "130px" },
                { label: "Reads", width: "90px" },
                { label: "Last read", width: "120px" },
              ]}
              rows={report.reads.map((r) => ({
                id: `${r.actor}/${r.agent}`,
                items: [
                  { label: <NameBadge name={r.actor} /> },
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
