import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import { gradeRun, type Identity } from "../api";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";

/** A closed run's verdict (run.grade): it held up, or it did not, once a person knows. */
export function RunGrade({ identity, runId }: { identity: Pick<Identity, "org" | "namespace">; runId: string }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [graded, setGraded] = useState<"pass" | "fail" | null>(null);
  const grade = async (g: "pass" | "fail") => {
    setBusy(true);
    try {
      await gradeRun(identity, runId, g, reason.trim());
      setGraded(g);
      toast(g === "pass" ? "Graded: held up" : "Graded: did not hold", "ok");
    } catch (e) {
      toast(`Grade failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };
  if (graded) return <p className="text-sm">{graded === "pass" ? "Graded as held up." : "Graded as not held."}</p>;
  return (
    <div className="flex flex-wrap items-end gap-2">
      <TextField id="run-grade-reason" label="Why (optional)" value={reason} onChange={setReason} />
      <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void grade("fail")}>
        Did not hold
      </Button>
      <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void grade("pass")}>
        Held up
      </Button>
    </div>
  );
}
