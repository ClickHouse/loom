import { useId } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Popover } from "@clickhouse/click-ui/Popover";
import { Title } from "@clickhouse/click-ui/Title";
import type { InboxCount } from "../api/tasks";
import { relativeTime } from "../lib/format";
import { useStore } from "../lib/state";
import { attentionByInbox, bellCount } from "../lib/taskBell";

/** The task bell's popover: the inboxes with something waiting, each opening the Tasks view
 *  on it. Loaded the first time the bell opens. */
export function TaskInboxList({
  counts,
  namespace: ns,
  close,
}: {
  counts: InboxCount[];
  namespace: string;
  close: () => void;
}) {
  const headingId = useId();
  const total = bellCount(counts);
  const inboxes = attentionByInbox(counts);
  const openInbox = (inbox: string) => {
    close();
    // A new entry for Tasks on that inbox: the page being left keeps its URL and state.
    useStore.getState().navigate("tasks", { query: { inbox } });
  };
  return (
    <Popover.Content
      align="end"
      aria-labelledby={headingId}
      className="w-[min(400px,92vw)] max-h-[480px] overflow-y-auto"
    >
      <Title type="h2" size="sm" id={headingId} className="pb-2 border-b border-border">
        Tasks waiting · {total}
      </Title>
      {inboxes.length === 0 && (
        <div className="py-6 text-sm text-text-muted text-center">
          Nothing waits for you.
          <div className="text-xs text-text-muted mt-1.5">
            Review items in triage and blocked tasks in the inboxes of <span className="font-mono">{ns}</span> show up
            here.
          </div>
          <Button
            type="secondary"
            htmlType="button"
            className="mt-3"
            onClick={() => {
              close();
              useStore.getState().setView("tasks");
            }}
          >
            Open tasks
          </Button>
        </div>
      )}
      {inboxes.map((a) => (
        <button
          type="button"
          key={a.inbox}
          onClick={() => openInbox(a.inbox)}
          className="text-left -mx-2 w-[calc(100%+1rem)] px-2 py-2.5 rounded-sm hover:bg-surface-2 flex items-start gap-3 transition-colors"
        >
          <Icon name="email" size="xs" aria-hidden="true" className="mt-1" />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium font-mono break-all">{a.inbox}</div>
            <div className="text-xs text-text-muted mt-0.5">
              {[
                a.triage > 0 ? `${a.triage} in triage` : "",
                a.blockedHuman > 0 ? `${a.blockedHuman} blocked on a person` : "",
                a.blockedOther > 0 ? `${a.blockedOther} blocked on a sender or external event` : "",
                a.oldest ? `oldest ${relativeTime(a.oldest)}` : "",
              ]
                .filter(Boolean)
                .join(" · ")}
            </div>
          </div>
          <span className="text-xs text-text-muted mt-1 shrink-0 inline-flex items-center gap-1">
            Open <Icon name="arrow-right" size="xs" aria-hidden="true" />
          </span>
        </button>
      ))}
    </Popover.Content>
  );
}
