import { Button } from "@clickhouse/click-ui/Button";
import { Unavailable } from "./ui/Unavailable";
import { loadDockRuntime, useDock, type AskItem } from "../lib/assistantDock";
import { canCall } from "../lib/canCall";
import { useNamespaceRole } from "../lib/namespaceRole";
import { useStore } from "../lib/state";

/** One item's words in the context line, on one line and cut, so a long key or query never
 *  crowds the question. */
export function clip(text: string, max = 200): string {
  const one = text.replace(/\s+/g, " ").trim();
  return one.length > max ? `${one.slice(0, max - 1)}…` : one;
}

/**
 * "Ask about this": a secondary button that names `item` in the assistant's next question
 * (the chip "Includes: <page> · <item>") and opens the panel with its field focused. It
 * sends nothing. `leave` runs first: a drawer or a dialog closes itself, with a flag so its
 * close hands the focus to nobody (the panel's field takes it). Shown to whoever the
 * assistant's route admits on the header's namespace; unavailable while the assistant
 * cannot answer, or for the caller's own `reason`.
 */
export function AskAboutThis({
  what,
  item,
  reason,
  leave,
  short = false,
}: {
  what: "memory" | "document" | "result";
  item: Omit<AskItem, "view" | "namespace">;
  reason?: string | null;
  leave?: () => void;
  /** "Ask" alone, for a table row; the name says the rest. */
  short?: boolean;
}) {
  const whoami = useStore((s) => s.whoami);
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  const role = useNamespaceRole(namespace);
  const unavailable = useDock((s) => s.reasonShort);
  if (!canCall("assistant.ask", role, whoami)) return null;
  const why = reason || (unavailable ? `The assistant can't answer now: ${unavailable}.` : null);
  return (
    <Unavailable reason={why}>
      <Button
        type="secondary"
        htmlType="button"
        iconLeft="sparkle"
        aria-label={`Ask the assistant about this ${what}`}
        onClick={(e) => {
          e.stopPropagation();
          const st = useStore.getState();
          const asked: AskItem = { ...item, view: st.view, namespace: st.identity?.namespace ?? "" };
          leave?.();
          // After the closing layer has gone, so its focus trap cannot take the focus back.
          window.setTimeout(() => void loadDockRuntime().then((m) => m.askAbout(asked)), 0);
        }}
      >
        {short ? "Ask" : "Ask about this"}
      </Button>
    </Unavailable>
  );
}
