import { Component, useEffect, useId, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Link } from "@clickhouse/click-ui/Link";
import { Select } from "@clickhouse/click-ui/Select";
import type {
  InspectSubject,
  KnowledgeInspection,
  MemoryInspection,
  MentorBriefResult,
  NamespaceAuditResult,
  NamespaceInventory,
  NamespaceTune,
} from "../../../api";
import { Loading, SectionHeading, Unavailable } from "../../../components/ui";
import { ListTime } from "../../../components/ui/ListTime";
import { openGraphAt } from "../../../lib/graphLink";
import { openMemoriesSearching } from "../../../lib/memoriesSearch";
import { useStore, type View } from "../../../lib/state";
import { useChat, type Turn } from "../chatStore";
import { DocumentInspection } from "../DocumentInspection";
import { Inspection } from "../Inspection";
import { CardLinked } from "./linked";
import { CardSection } from "./section";
import { InventoryPopulations, InventoryScopeNote, InventorySections } from "../Inventory";
import { INSPECTED_AS } from "../subjects";
import { AuditBody } from "./AuditBody";
import { ROLE_LOADING } from "../../../lib/callReason";
import { isFresh, readCard, startStarter, useCardGate } from "./cardStore";
import { MentorBody } from "./MentorBody";
import { askNow, asks, useStarterPrefs } from "./prefs";
import { AUDIT_WINDOWS, MENTOR_WINDOWS, DEFAULT_WINDOW, starterOf } from "./starters";
import { TuneBody } from "./TuneBody";
import type { CardArgs, CardOp, TurnCard } from "./types";

/** A card whose read came back in a shape its body cannot draw says so in its place; the
 *  conversation and the page around it stay. */
class CardBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <p className="text-sm text-text-muted">This read could not be shown. Read it again.</p>
    ) : (
      this.props.children
    );
  }
}

/** "Open in <page>": the page that owns what a card shows. */
function OpenIn({ page, onOpen }: { page: string; onOpen: () => void }) {
  return (
    <Link component="button" type="button" size="sm" onClick={onOpen}>
      {`Open in ${page}`}
    </Link>
  );
}

const go = (view: View, query?: Record<string, string>) => useStore.getState().navigate(view, query ? { query } : {});

/** The footer link of a card, by its op and arguments. */
function footer(card: TurnCard): ReactNode {
  switch (card.op) {
    case "namespace.inventory":
    case "namespace.audit":
      return <OpenIn page="Memories" onOpen={() => go("memories")} />;
    case "memory.inspect":
      return card.args.subject === "agent" && card.args.id ? (
        <OpenIn
          page="Memories"
          onOpen={() => openMemoriesSearching(`agent:${card.args.id}`, () => go("memories", {}))}
        />
      ) : (
        <OpenIn page="Memories" onOpen={() => go("memories")} />
      );
    case "knowledge.inspect":
      return <OpenIn page="Library" onOpen={() => go("knowledge")} />;
    case "namespace.tune":
      return <OpenIn page="Settings" onOpen={() => go("setup", { tab: "structure" })} />;
    default:
      return null;
  }
}

/** The selects a card re-reads with, with no model call: the audit's window, the mentor's
 *  agent and window. */
function Controls({ card, reread }: { card: TurnCard; reread: (args: CardArgs) => void }) {
  if (card.op === "namespace.audit")
    return (
      <div className="w-40">
        <Select
          label="Window"
          triggerProps={{ "aria-label": "Audit window" }}
          value={String(card.args.windowSeconds ?? DEFAULT_WINDOW)}
          onSelect={(v) => reread({ windowSeconds: Number(v) })}
          disabled={card.busy}
          options={AUDIT_WINDOWS.map((w) => ({ value: String(w.seconds), label: w.label }))}
        />
      </div>
    );
  if (card.op === "mentor.brief") {
    const r = card.data as MentorBriefResult | undefined;
    return (
      <div className="flex flex-wrap items-end gap-3">
        {r && r.agents.length > 0 && (
          <div className="w-full sm:w-56">
            <Select
              label="Agent"
              value={card.args.agent || r.agent}
              onSelect={(v) => reread({ agent: v })}
              disabled={card.busy}
              options={[r.agent, ...r.agents.filter((a) => a !== r.agent)].map((a) => ({ value: a, label: a }))}
            />
          </div>
        )}
        <div className="w-40">
          <Select
            label="Window"
            triggerProps={{ "aria-label": "Mentor window" }}
            value={String(card.args.windowSeconds ?? DEFAULT_WINDOW)}
            onSelect={(v) => reread({ windowSeconds: Number(v) })}
            disabled={card.busy}
            options={MENTOR_WINDOWS.map((w) => ({ value: String(w.seconds), label: w.label }))}
          />
        </div>
      </div>
    );
  }
  return null;
}

function Body({
  turn,
  card,
  gate,
  head,
  sub,
}: {
  turn: Turn;
  card: TurnCard;
  gate: (op: CardOp) => string | null;
  /** The card heading's id, which its sections' ids start with. */
  head: string;
  /** Its sections' heading level. */
  sub: 3 | 4;
}) {
  const namespace = useChat((s) => s.namespace) ?? "";
  const answering = useChat((s) => s.answering);
  const asking = useStarterPrefs(asks);
  switch (card.op) {
    case "namespace.inventory": {
      const inv = card.data as NamespaceInventory;
      // A pick is the subject starter for that id, asked as the checkbox says; with the box on
      // and nothing to ask now, its card says why, as a starter's does.
      const pick = (subject: InspectSubject, id: string) => {
        const now = askNow();
        startStarter(
          "memory.inspect",
          { subject, id },
          { alsoAsk: now.asks, gate: gate("memory.inspect"), namespace, unanswered: now.unanswered },
        );
      };
      // A pick is off as the subject starter is: while an answer comes in, and when its read
      // is refused and nothing would be asked (asking, the card shows the refusal).
      const blocked = answering ? "An answer is still coming in." : asking ? null : gate("memory.inspect");
      // Only users and agents are picked; authors and members are listed, not inspected.
      const pickable = inv.populations.some((p) => INSPECTED_AS[p.population] && p.top.length > 0);
      return (
        <div className="space-y-3">
          <InventoryScopeNote inventory={inv} />
          <section aria-labelledby={`${head}-holds`}>
            <SectionHeading id={`${head}-holds`} level={sub} label="What this namespace holds" />
            <InventorySections inventory={inv} />
          </section>
          <section aria-labelledby={`${head}-people`}>
            <SectionHeading
              id={`${head}-people`}
              level={sub}
              label="Who appears here"
              hint={pickable && !blocked ? "Pick a user or agent to see what it remembers." : undefined}
            />
            <InventoryPopulations inventory={inv} onPick={pick} pickBlocked={blocked} />
          </section>
        </div>
      );
    }
    case "namespace.audit":
      return <AuditBody result={card.data as NamespaceAuditResult} />;
    case "mentor.brief":
      return <MentorBody result={card.data as MentorBriefResult} namespace={namespace} />;
    case "memory.inspect":
      return <Inspection result={card.data as MemoryInspection} />;
    case "knowledge.inspect":
      return <DocumentInspection result={card.data as KnowledgeInspection} />;
    case "namespace.tune":
      return <TuneBody tune={card.data as NamespaceTune} fresh={isFresh(turn.id)} readAt={card.readAt} />;
    case "graph":
      return null;
  }
}

/**
 * One card in the conversation: Loom's own read for a starter, with the person's token. It
 * says what it is ("Loom's own read · <time>", or "Read <time>" once restored from History),
 * reads again on request, and links to the page that owns it. Its heading sits one level
 * under the panel's (`level`). Landing, it never moves the focus.
 */
export function Card({ turn, level }: { turn: Turn; level: 2 | 3 }) {
  const card = turn.card as TurnCard;
  const namespace = useChat((s) => s.namespace) ?? "";
  const header = useStore((s) => s.identity?.namespace ?? "");
  // A page or the drawer shows the header's namespace: a card read in another opens nothing.
  const linked = namespace === header;
  const gate = useCardGate(namespace);
  const head = useId();
  const starter = starterOf(card.op);
  const fresh = isFresh(turn.id);
  const blockedNow = gate(card.op);
  const reread = (args?: CardArgs) => readCard(turn.id, blockedNow, args);
  // A turn asked again reads its card again, once the person's role is known.
  useEffect(() => {
    if (card.again && blockedNow !== ROLE_LOADING) readCard(turn.id, blockedNow);
  }, [card.again, blockedNow, turn.id]);
  const thing = starter.title.toLowerCase();
  // The body's sections sit one level under the card's heading.
  const sub = level === 2 ? 3 : 4;
  // A subject's card is named with its subject too (the line Inspection draws), so two of
  // them in one conversation read apart.
  const named = card.op === "memory.inspect" && card.data !== undefined ? `${head} ${head}-subject` : head;
  return (
    <section
      aria-labelledby={named}
      aria-busy={card.busy || undefined}
      className="pane-card space-y-3 text-sm"
      data-testid="assistant-card"
      data-card={card.op}
    >
      <SectionHeading id={head} level={level} label={starter.title} />
      <Controls card={card} reread={reread} />
      {card.unanswered && (
        <p className="text-xs text-text-muted">{`The assistant can't answer: ${card.unanswered}. This is Loom's own read.`}</p>
      )}
      {!linked && (
        <p className="text-xs text-text-muted">{`Read in ${namespace}: switch to it to open what this card names.`}</p>
      )}
      {card.op === "graph" ? (
        <div className="space-y-2">
          <p className="text-text-muted">The graph shows how the facts in your memories connect.</p>
          <Button
            disabled={!linked}
            type="secondary"
            htmlType="button"
            onClick={() => openGraphAt(card.args.entity ?? "", { from: "Assistant" })}
          >
            {card.args.entity ? `Open the graph at ${card.args.entity}` : "Open the graph"}
          </Button>
        </div>
      ) : card.busy && card.data === undefined ? (
        <Loading layout="inline" label={`Loading the ${thing}…`} />
      ) : card.error ? (
        <p
          className={card.failed ? "text-sm text-danger" : "text-sm text-text-muted"}
          role={card.failed ? "alert" : undefined}
          data-testid="assistant-card-refused"
        >
          {card.error}
        </p>
      ) : card.data !== undefined ? (
        <CardBoundary key={card.readAt}>
          <CardLinked.Provider value={linked}>
            <CardSection.Provider value={{ prefix: head, level: sub }}>
              <Body turn={turn} card={card} gate={gate} head={head} sub={sub} />
            </CardSection.Provider>
          </CardLinked.Provider>
        </CardBoundary>
      ) : card.readAt ? (
        <p className="text-sm text-text-muted">This read was too large to keep in this browser: read it again.</p>
      ) : null}
      {card.op !== "graph" && (
        <div className="flex flex-wrap items-center gap-3 text-xs text-text-muted">
          {card.readAt && (
            <span data-testid="assistant-card-provenance">
              {fresh ? "Loom's own read · " : "Read "}
              <ListTime time={new Date(card.readAt).toISOString()} />
            </span>
          )}
          <Unavailable reason={gate(card.op)}>
            {/* Never disabled while it reads: a read again replaces the one in flight, and the
                focus stays on the button. */}
            <Button type="secondary" htmlType="button" onClick={() => reread()}>
              Read again
            </Button>
          </Unavailable>
          {linked && footer(card)}
        </div>
      )}
    </section>
  );
}
