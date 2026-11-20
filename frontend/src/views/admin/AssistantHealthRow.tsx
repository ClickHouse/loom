import { useEffect, useState } from "react";
import { Alert } from "../../components/ui";
import { healthProblems } from "../../components/ProblemsBanner";
import { hasPlatformAdminClaim } from "../../lib/role";
import { sharedHealth } from "../../lib/sharedReads";
import { useStore } from "../../lib/state";
import { readAssistantCheck, type AssistantCheck } from "../assistant/readiness";

const SAID: Record<AssistantCheck, string> = {
  ready: "The assistant is ready.",
  not_configured: "No LibreChat is configured for the assistant (LOOM_ASSISTANT_LIBRECHAT_URL).",
  starting: "The assistant is setting up; this usually takes under a minute.",
  failing: "The assistant's setup failed. Loom retries the setup on its own.",
};

/**
 * Health's Assistant row: this server's readiness check for the assistant and, when
 * `/v1/health` lists the assistant's problem, its fix. A platform admin sees it whenever
 * readiness can be read; anyone else only while the assistant sets up or fails. The
 * assistant panel's "see Health" links land on it (`data-focus-key="health-assistant"`);
 * while its read is in flight it marks itself pending, so the arrival waits for it rather
 * than for a timeout.
 */
export function AssistantHealthRow() {
  const whoami = useStore((s) => s.whoami);
  const platform = hasPlatformAdminClaim(whoami);
  const [check, setCheck] = useState<AssistantCheck | null | "reading">("reading");
  const [fix, setFix] = useState("");
  useEffect(() => {
    const ctl = new AbortController();
    void readAssistantCheck(ctl.signal).then((c) => {
      if (!ctl.signal.aborted) setCheck(c);
    });
    sharedHealth()
      .then((h) => {
        const p = h ? healthProblems(h).find((x) => x.code === "assistant_service_failing") : undefined;
        if (!ctl.signal.aborted) setFix(p ? p.fix : "");
      })
      .catch(() => {
        /* no fix sentence: the row says what readiness says */
      });
    return () => ctl.abort();
  }, []);
  if (check === "reading") return <span data-focus-pending="health-assistant" hidden />;
  if (check === null) return null;
  const shown = platform || check === "starting" || check === "failing";
  if (!shown) return null;
  return (
    <section data-focus-key="health-assistant" tabIndex={-1} className="mb-5" aria-label="Assistant">
      <Alert
        state={check === "ready" ? "success" : check === "starting" ? "info" : "warning"}
        text={
          <>
            <strong>Assistant:</strong> {SAID[check]}
            {fix && check === "failing" && <span className="block mt-1 break-words">{fix}</span>}
          </>
        }
      />
    </section>
  );
}
