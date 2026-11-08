import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import { gradeRun, type Identity, type RunRow } from "../api";
import { humanizeError } from "../lib/format";
import { useStore } from "../lib/state";
import { toast } from "../components/Toast";

const VERDICT: Record<string, string> = { pass: "held up", fail: "did not hold" };

/** A closed run's verdict (run.grade): it held up, or it did not, once a person knows. A
 *  run already graded shows its verdict, and a new one replaces it. */
export function RunGrade({ identity, run }: { identity: Pick<Identity, "org" | "namespace">; run: RunRow }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [current, setCurrent] = useState({ grade: run.grade, source: run.grade_source, reason: run.grade_reason });
  // The grade's source names who graded; the server takes at most 64 characters.
  const grader = (useStore((s) => s.whoami?.subject) || "dashboard").slice(0, 64);
  const grade = async (g: "pass" | "fail") => {
    setBusy(true);
    try {
      const r = await gradeRun(identity, run.run_id, g, reason.trim(), grader);
      if (r.skipped_active) toast("The run is still active; it can be graded once it ends.", "info");
      else if (r.unchanged) toast(`Already graded as ${VERDICT[g]}`, "info");
      else {
        setCurrent({ grade: g, source: grader, reason: reason.trim() });
        // Under an owner's auto policy a grade also accepts or rejects proposals.
        const decided = r.proposals_decided.length;
        toast(
          `Graded: ${VERDICT[g]}${decided ? `, ${decided} proposal${decided === 1 ? "" : "s"} decided` : ""}`,
          "ok",
        );
      }
    } catch (e) {
      toast(`Grade failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-2">
      {current.grade && (
        <p className="text-sm">
          Graded as {VERDICT[current.grade] ?? current.grade}
          {current.source && ` by ${current.source}`}
          {current.reason && `: ${current.reason}`}
        </p>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <TextField id="run-grade-reason" label="Why (optional)" value={reason} onChange={setReason} />
        <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void grade("fail")}>
          {current.grade ? "Regrade: did not hold" : "Did not hold"}
        </Button>
        <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void grade("pass")}>
          {current.grade ? "Regrade: held up" : "Held up"}
        </Button>
      </div>
    </div>
  );
}
