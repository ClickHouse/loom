import { useId, useState } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import { Table } from "@clickhouse/click-ui/Table";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { Title } from "@clickhouse/click-ui/Title";
import {
  listPolicies,
  setPolicy,
  deletePolicy,
  checkPolicy,
  listPolicyDecisions,
  type PolicyRule,
  type PolicyMode,
  type PolicyEffect,
  type PolicyCondition,
  type PolicyCheckResponse,
  type PolicyDecisionRow,
  type PolicyDecisionKind,
} from "../../api";
import { useIdentity } from "../../lib/state";
import { IconButton } from "../../components/IconButton";
import { Modal, ModalFooter } from "../../components/ui/Modal";
import { Panel } from "../../components/ui/Panel";
import { Skeleton, SkeletonCard } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast, toastError } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { humanizeError, sentenceCase } from "../../lib/format";
import { AgentBadge } from "../../components/AgentBadge";
import { useAbortableEffect } from "../../lib/hooks";
import { PageContainer } from "../../layout/PageContainer";
import { oneOf } from "../../lib/oneOf";
import { isRecord } from "../../lib/guards";
import { Alert, ListTime, LoadingSkeleton, PageHeader } from "../../components/ui";
import { ListState } from "../../components/ui/ListState";

/**
 * Policies — history rules over what an agent may do next.
 *
 * A rule is `effect` (forbid | require) + `actions` (operation names or
 * globs) + a `condition` over the caller's own recent trace. The gate
 * evaluates every matching rule before a call runs; LOOM_POLICY_MODE
 * decides whether a deny is logged (shadow) or returned (enforce). This
 * view manages the rules, dry-runs a hypothetical call against them, and
 * reads the decision ledger.
 */

const EFFECTS: readonly PolicyEffect[] = ["forbid", "require"];

const MODE_META: Record<PolicyMode, { state: BadgeState; blurb: string }> = {
  off: {
    state: "neutral",
    blurb: "Rules are stored but never evaluated. Nothing is logged or blocked.",
  },
  shadow: {
    state: "warning",
    blurb: "Every matching call is evaluated and recorded below, but a deny does not block the call.",
  },
  enforce: {
    state: "danger",
    blurb: "A deny blocks the call with 403 POLICY_FORBIDDEN. Rule errors on write-shaped calls also deny.",
  },
};

const MODES: readonly PolicyMode[] = ["off", "shadow", "enforce"];

/** Badge style and blurb for a server-reported mode; an unknown mode reads as off. */
function modeMeta(mode: string) {
  return MODE_META[oneOf(MODES, mode) ?? "off"];
}

function ModeBadge({ mode }: { mode: string }) {
  return <Badge text={`mode: ${mode}`} state={modeMeta(mode).state} size="sm" />;
}

function DecisionChip({ decision }: { decision: string }) {
  const deny = decision === "deny";
  return <Badge text={sentenceCase(decision)} state={deny ? "danger" : "success"} size="sm" />;
}

/** A rule's condition as one summary line; the button opens the full JSON under it. */
function Condition({ condition }: { condition: unknown }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="font-mono text-xs">
      <button
        type="button"
        className="inline-flex items-start gap-1 text-left cursor-pointer"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon name="chevron-right" size="sm" aria-hidden="true" className={open ? "rotate-90" : ""} />
        {summariseCondition(condition)}
      </button>
      <pre id={id} hidden={!open} className="mt-1 whitespace-pre-wrap break-words">
        {JSON.stringify(condition, null, 2)}
      </pre>
    </div>
  );
}

const DASH = <span className="text-text-muted">—</span>;

function Bool({ v }: { v: boolean | null | undefined }) {
  if (v === null || v === undefined) return DASH;
  // A yes/no is not a status: plain text, the colour left to the decision badges.
  return <span className={v ? "text-text" : "text-text-muted"}>{v ? "Yes" : "No"}</span>;
}

const CMP_SYMBOL: Record<string, string> = { gt: ">", gte: "≥", lt: "<", lte: "≤", eq: "=" };

function opList(v: unknown): string {
  if (Array.isArray(v)) return v.map(String).join("|");
  return v == null ? "?" : String(v);
}

/** One-line rendering of a condition tree, e.g.
 *  "formerly memory.get within 1h" / "count_within memory.set ≥ 200 in 1h"
 *  / "not_since memory.delete since memory.set in 24h". Combinators nest
 *  with all(...) / any(...) / not(...). */
function summariseCondition(n: unknown): string {
  if (!isRecord(n)) return String(n ?? "");
  const op = String(n.op ?? "?");
  if (op === "all" || op === "any") {
    const of = Array.isArray(n.of) ? n.of : [];
    return `${op}(${of.map(summariseCondition).join(op === "all" ? " AND " : " OR ")})`;
  }
  if (op === "not") return `not(${summariseCondition(n.of)})`;
  const suffix: string[] = [];
  if (n.key) suffix.push(`same ${String(n.key)}`);
  if (n.success === true) suffix.push("succeeded");
  if (n.success === false) suffix.push("failed");
  if (isRecord(n.args_equal)) {
    suffix.push(
      Object.entries(n.args_equal)
        .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
        .join(", "),
    );
  }
  const tail = suffix.length ? ` (${suffix.join(", ")})` : "";
  const within = String(n.within ?? "?");
  if (op === "formerly") return `formerly ${opList(n.operation)} within ${within}${tail}`;
  if (op === "count_within")
    return `count_within ${opList(n.operation)} ${CMP_SYMBOL[String(n.cmp)] ?? String(n.cmp ?? "?")} ${String(n.value ?? "?")} in ${within}${tail}`;
  if (op === "not_since") return `not_since ${opList(n.operation)} since ${opList(n.anchor)} in ${within}${tail}`;
  return JSON.stringify(n);
}

const EXAMPLE_CONDITION = JSON.stringify(
  {
    op: "formerly",
    operation: "memory.get",
    within: "1h",
    key: "agent",
    args_equal: { memory_key: "$args.memory_key" },
  },
  null,
  2,
);

const GRAMMAR_HELP =
  'Leaves: {"op":"formerly"|"count_within"|"not_since", "within":"1h" (s/m/h/d), "operation": string|string[], ' +
  '"key"?: "agent"|"session"|"user", "success"?: bool, "args_equal"?: {field: literal|"$args.<field>"}}. ' +
  'count_within adds "cmp": gt|gte|lt|lte|eq and "value": int; not_since adds "anchor": string|string[]. ' +
  'Combinators: {"op":"all"|"any","of":[…]}, {"op":"not","of":{…}}.';

function Card({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <Panel
      as="section"
      aria-label={title}
      header={
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Title type="h2" size="sm">
            {title}
          </Title>
          {action}
        </div>
      }
      bodyClassName="pt-3"
    >
      {children}
    </Panel>
  );
}

function parseJsonObject(text: string, what: string): Record<string, unknown> {
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch (e) {
    throw new Error(`${what} is not valid JSON: ${humanizeError(e)}`);
  }
  if (!isRecord(v)) throw new Error(`${what} must be a JSON object`);
  return v;
}

export function Policies() {
  const identity = useIdentity();
  const [rules, setRules] = useState<PolicyRule[] | null>(null);
  // The server's mode, unknown until a read answers: none is claimed before then.
  const [mode, setMode] = useState<PolicyMode | string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [decisionsVersion, setDecisionsVersion] = useState(0);

  // A reload after an add or a delete keeps the rules on screen, marked busy.
  const [refreshing, setRefreshing] = useState(false);
  // Another namespace's rules are not this one's: start from the skeleton. A reload after
  // a write runs through this effect too, so an answer for a namespace left is dropped.
  const [reads, setReads] = useState(0);
  const scope = `${identity.org}/${identity.namespace}`;
  // During render, so the old rules (and their Delete buttons) never paint under it.
  const [rulesFor, setRulesFor] = useState(scope);
  if (rulesFor !== scope) {
    setRulesFor(scope);
    setRules(null);
    setError(null);
  }
  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setRefreshing(true);
      listPolicies({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (live()) {
            setRules(r.rules || []);
            setMode(r.mode || "off");
          }
        })
        .catch((e) => {
          // Not an empty list: the error says the read failed, with Retry.
          if (live()) {
            setError(e);
            setRules(null);
          }
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [scope, reads],
  );

  const remove = async (r: PolicyRule) => {
    if (
      !(await confirmAction({
        title: `Delete rule "${r.policy_id}"?`,
        message:
          "The rule stops applying to every writer in this namespace immediately. Past decisions stay in the ledger.",
        confirmLabel: "Delete",
        danger: true,
      }))
    )
      return;
    try {
      await deletePolicy({ org: identity.org, namespace: identity.namespace }, r.policy_id);
      toast("Rule deleted", "ok");
      setReads((n) => n + 1);
    } catch (e) {
      toastError("Failed", e);
    }
  };

  const meta = mode === null ? null : modeMeta(mode);

  return (
    <PageContainer>
      <PageHeader title="Policies">
        <ScopeBadge kind="namespace" value={identity.namespace} />
        {mode !== null && <ModeBadge mode={mode} />}
      </PageHeader>
      <p className="text-sm text-text-muted mb-5 max-w-[80ch]">
        Rules over an agent's own recent history: forbid or require an action based on what the same caller did in the
        last minutes or hours. {meta && <span className="text-text">{meta.blurb}</span>} The mode is the server's{" "}
        <code className="code-inline text-xs">LOOM_POLICY_MODE</code>; it cannot be changed here.
      </p>

      <div className="space-y-4">
        <Card
          title="Rules"
          action={
            <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAddOpen(true)}>
              Add rule
            </Button>
          }
        >
          {/* Mounted before the read answers, so the count is announced when it lands. */}
          <p className="mb-2 text-xs text-text-muted tabular" aria-live="polite">
            {rules !== null && !error ? `${rules.length} ${rules.length === 1 ? "rule" : "rules"}` : ""}
          </p>
          {rules === null && !error && (
            <LoadingSkeleton label="Loading rules">
              <Skeleton className="h-24 w-full" />
            </LoadingSkeleton>
          )}
          {!!error && <ListState kind="error" things="rules" error={error} onRetry={() => setReads((n) => n + 1)} />}
          {rules !== null && !error && rules.length === 0 && (
            <EmptyState
              icon="secure"
              title="No rules"
              description="Add a rule to forbid or require an action based on the caller's recent history. Rules only take effect once the server mode leaves off."
            />
          )}
          {rules !== null && !error && rules.length > 0 && (
            <div aria-busy={refreshing || undefined}>
              <Table
                size="sm"
                headers={[
                  { label: "Policy" },
                  { label: "Effect", width: "96px" },
                  { label: "Actions" },
                  { label: "Condition" },
                  { label: "Enabled", width: "80px" },
                  { label: "Description" },
                  { label: <span className="sr-only">Delete</span>, width: "56px" },
                ]}
                rows={rules.map((r) => ({
                  id: r.policy_id,
                  items: [
                    {
                      label: (
                        <div className="font-mono text-xs">
                          <div>{r.policy_id}</div>
                          {r.updated_at && (
                            <div className="text-xs font-sans text-text-muted">
                              updated <ListTime time={r.updated_at} quiet />
                            </div>
                          )}
                        </div>
                      ),
                    },
                    {
                      label: (
                        <Badge
                          text={sentenceCase(r.effect)}
                          state={r.effect === "forbid" ? "danger" : "neutral"}
                          size="sm"
                        />
                      ),
                    },
                    { label: <span className="font-mono text-xs">{r.actions.join(", ")}</span>, overflowMode: "wrap" },
                    {
                      label: <Condition condition={r.condition} />,
                      overflowMode: "wrap",
                    },
                    { label: <Bool v={r.enabled} /> },
                    {
                      label: r.description ? <span className="text-text-muted">{r.description}</span> : DASH,
                      overflowMode: "wrap",
                    },
                    {
                      label: (
                        <IconButton
                          icon="trash"
                          type="danger"
                          size="sm"
                          label={`Delete ${r.policy_id}`}
                          onClick={() => remove(r)}
                        />
                      ),
                    },
                  ],
                }))}
              />
            </div>
          )}
        </Card>

        <DryRunPanel onRan={() => setDecisionsVersion((v) => v + 1)} />

        <DecisionsPanel version={decisionsVersion} />
      </div>

      <AddRuleDialog
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onSaved={() => {
          setAddOpen(false);
          setReads((n) => n + 1);
        }}
      />
    </PageContainer>
  );
}

function AddRuleDialog({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved: () => void }) {
  const identity = useIdentity();
  const [policyId, setPolicyId] = useState("");
  const [effect, setEffect] = useState<PolicyEffect>("forbid");
  const [actions, setActions] = useState("memory.delete");
  const [description, setDescription] = useState("");
  const [condition, setCondition] = useState(EXAMPLE_CONDITION);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const reset = () => {
    setPolicyId("");
    setEffect("forbid");
    setActions("memory.delete");
    setDescription("");
    setCondition(EXAMPLE_CONDITION);
    setErr(null);
  };

  const close = () => {
    onClose();
    setErr(null);
  };

  const submit = async () => {
    setErr(null);
    const id = policyId.trim();
    const acts = actions
      .split(",")
      .map((a) => a.trim())
      .filter(Boolean);
    if (!id) {
      setErr("policy_id is required");
      return;
    }
    if (acts.length === 0) {
      setErr("at least one action is required");
      return;
    }
    let cond: PolicyCondition;
    try {
      cond = parseJsonObject(condition, "condition");
    } catch (e) {
      setErr(humanizeError(e));
      return;
    }
    setBusy(true);
    try {
      const out = await setPolicy(
        { org: identity.org, namespace: identity.namespace },
        { policy_id: id, effect, actions: acts, condition: cond, description: description.trim() || undefined },
      );
      toast(`Rule "${out.rule.policy_id}" saved (${out.leaf_count} leaf${out.leaf_count === 1 ? "" : "s"})`, "ok");
      reset();
      onSaved();
    } catch (e) {
      // 422 VALIDATION_FAILED carries the parser's message ("policy rule
      // rejected: ..."); keep it inline so the JSON can be fixed in place.
      setErr(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onOpenChange={(v) => {
        if (!v) close();
      }}
      title="Add rule"
      description="Saving under an existing policy_id replaces that rule. Rules apply to every writer in this namespace."
      className="w-full max-w-[640px]"
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-[1fr_140px] gap-3">
            <TextField
              label="Policy ID"
              value={policyId}
              onChange={(value) => setPolicyId(value)}
              placeholder="no-blind-delete"
              className="font-mono"
            />
            <Select
              label="Effect"
              value={effect}
              onSelect={(value) => {
                const v = oneOf(EFFECTS, value);
                if (v !== null) setEffect(v);
              }}
            >
              {EFFECTS.map((x) => (
                <Select.Item key={x} value={x}>
                  {x}
                </Select.Item>
              ))}
            </Select>
          </div>
          <TextField
            label="Actions (comma-separated operation names or globs)"
            value={actions}
            onChange={(value) => setActions(value)}
            placeholder="memory.delete, memory.*"
            className="font-mono"
          />
          <TextField
            label="Description"
            value={description}
            onChange={(value) => setDescription(value)}
            placeholder="Optional — why this rule exists"
          />
          <div>
            <TextAreaField
              label="Condition (JSON)"
              value={condition}
              onChange={(value) => setCondition(value)}
              rows={8}
              spellCheck={false}
              className="font-mono"
            />
            <div className="text-xs text-text-muted mt-1.5 leading-relaxed">
              <span className="font-medium text-text">forbid</span> denies when the condition holds;{" "}
              <span className="font-medium text-text">require</span> denies when it does not. {GRAMMAR_HELP}
            </div>
          </div>
          {err && (
            <Alert
              role="alert"
              state="danger"
              size="small"
              text={<span className="whitespace-pre-wrap break-words">{err}</span>}
            />
          )}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={close}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" iconLeft="plus" loading={busy}>
            Save rule
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}

function DryRunPanel({ onRan }: { onRan: () => void }) {
  const identity = useIdentity();
  const [operation, setOperation] = useState("memory.delete");
  const [agent, setAgent] = useState(identity.agent || "");
  const [args, setArgs] = useState('{\n  "memory_key": "example"\n}');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<PolicyCheckResponse | null>(null);

  const run = async () => {
    setErr(null);
    const op = operation.trim();
    if (!op) {
      setErr("operation is required");
      return;
    }
    let parsed: Record<string, unknown>;
    try {
      parsed = args.trim() ? parseJsonObject(args, "args") : {};
    } catch (e) {
      setErr(humanizeError(e));
      return;
    }
    setBusy(true);
    try {
      const out = await checkPolicy(
        { org: identity.org, namespace: identity.namespace },
        { operation: op, agent: agent.trim() || undefined, args: parsed },
      );
      setResult(out);
      onRan();
    } catch (e) {
      setResult(null);
      setErr(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Dry run">
      <div className="text-xs text-text-muted mb-3">
        Evaluate the stored rules against a hypothetical call, using the same evaluator and trace the gate reads.
        Nothing is blocked; <span className="font-mono">would_enforce</span> says what enforce mode would do.
      </div>
      <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_2fr] gap-3 items-start">
        <TextField
          label="Operation"
          value={operation}
          onChange={(value) => setOperation(value)}
          placeholder="memory.delete"
          className="font-mono"
        />
        <TextField
          label="Agent"
          value={agent}
          onChange={(value) => setAgent(value)}
          placeholder="- (none)"
          className="font-mono"
        />
        <TextAreaField
          label="Args (JSON)"
          value={args}
          onChange={(value) => setArgs(value)}
          rows={3}
          spellCheck={false}
          className="font-mono"
        />
      </div>
      <div className="mt-3 flex items-center gap-3 flex-wrap">
        <Button type="secondary" htmlType="button" iconLeft="play" onClick={run} loading={busy}>
          Check
        </Button>
        {err && <span className="text-xs text-danger break-words">{err}</span>}
      </div>

      {result && (
        <div className="mt-4 space-y-3">
          <div className="flex items-center gap-2 flex-wrap text-xs text-text-muted">
            <DecisionChip decision={result.decision} />
            <span>
              would_enforce <Bool v={result.would_enforce} />
            </span>
            <span>·</span>
            <span>mode {result.mode}</span>
            <span>·</span>
            <span>
              write-shaped <Bool v={result.write_shaped} />
            </span>
            <span>·</span>
            <span className="tabular-nums">{result.latency_ms} ms</span>
            {result.determining_rules.length > 0 && (
              <>
                <span>·</span>
                <span>
                  determined by <span className="font-mono text-text">{result.determining_rules.join(", ")}</span>
                </span>
              </>
            )}
          </div>
          {result.errors.length > 0 && (
            <Alert
              state="danger"
              size="small"
              text={
                <span className="block space-y-0.5">
                  {result.errors.map((e, i) => (
                    <span key={i} className="block break-words">
                      {e}
                    </span>
                  ))}
                </span>
              }
            />
          )}
          {result.rules.length === 0 && (
            <div className="text-xs text-text-muted">
              No rule matches <span className="font-mono">{operation.trim()}</span>.
            </div>
          )}
          {result.rules.length > 0 && (
            // Native: a rule spans one row per leaf (rowSpan), which Click UI's Table has no form for.
            <div className="rounded-lg border border-border overflow-x-auto">
              <table className="native-table text-sm">
                <thead>
                  <tr>
                    <th scope="col">Rule</th>
                    <th scope="col">Effect</th>
                    <th scope="col">Holds</th>
                    <th scope="col">Denies</th>
                    <th scope="col">Leaf op</th>
                    <th scope="col">Leaf holds</th>
                    <th scope="col">Value</th>
                  </tr>
                </thead>
                <tbody>
                  {result.rules.map((v) => {
                    const leaves = v.leaves.length ? v.leaves : [null];
                    return leaves.map((lf, i) => (
                      <tr key={`${v.policy_id}-${i}`} className="border-t border-border">
                        {i === 0 && (
                          <>
                            <td rowSpan={leaves.length} className="font-mono text-xs whitespace-nowrap align-top">
                              {v.policy_id}
                            </td>
                            <td rowSpan={leaves.length} className="align-top">
                              {v.effect}
                            </td>
                            <td rowSpan={leaves.length} className="align-top">
                              <Bool v={v.holds} />
                            </td>
                            <td rowSpan={leaves.length} className="align-top">
                              <Bool v={v.denies} />
                            </td>
                          </>
                        )}
                        <td className="font-mono text-xs">{lf ? lf.op : DASH}</td>
                        <td>
                          {lf ? (
                            lf.skipped ? (
                              <span className="text-text-muted">skipped</span>
                            ) : lf.error ? (
                              <span className="text-danger">error</span>
                            ) : (
                              <Bool v={lf.holds} />
                            )
                          ) : (
                            DASH
                          )}
                        </td>
                        <td className="font-mono text-xs max-w-[32ch]">
                          {lf ? (
                            <div className="break-words">
                              {lf.error ||
                                lf.skipped ||
                                (lf.value === null || lf.value === undefined
                                  ? "—"
                                  : typeof lf.value === "string"
                                    ? lf.value
                                    : JSON.stringify(lf.value))}
                            </div>
                          ) : (
                            DASH
                          )}
                        </td>
                      </tr>
                    ));
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

type DecisionFilter = "" | PolicyDecisionKind;

const DECISION_FILTERS: readonly DecisionFilter[] = ["", "deny", "allow"];
const FILTER_LABEL: Record<string, string> = { all: "All", deny: "Deny", allow: "Allow" };

function DecisionsPanel({ version }: { version: number }) {
  const identity = useIdentity();
  const [rows, setRows] = useState<PolicyDecisionRow[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  // Retry reads again through the effect.
  const [reads, setReads] = useState(0);
  const [filter, setFilter] = useState<DecisionFilter>("");
  // A new rules version keeps the ledger on screen, marked busy; another namespace or
  // filter is another list, so it starts from the skeleton.
  const [refreshing, setRefreshing] = useState(false);
  const rowsKey = `${identity.org}/${identity.namespace}/${filter}`;
  const [rowsFor, setRowsFor] = useState(rowsKey);
  if (rowsFor !== rowsKey) {
    setRowsFor(rowsKey);
    setRows(null);
    setError(null);
  }

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setRefreshing(true);
      listPolicyDecisions(
        { org: identity.org, namespace: identity.namespace },
        { decision: filter || undefined, limit: 100, signal },
      )
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e) => {
          if (live()) {
            setError(e);
            setRows(null);
          }
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [identity.org, identity.namespace, filter, version, reads],
  );

  return (
    <Card
      title="Decisions"
      action={
        <ButtonGroup
          aria-label="Filter decisions"
          options={DECISION_FILTERS.map((f) => ({ value: f || "all", label: FILTER_LABEL[f || "all"] }))}
          selected={filter || "all"}
          onClick={(value) => {
            const f = DECISION_FILTERS.find((x) => (x || "all") === value);
            if (f !== undefined) setFilter(f);
          }}
        />
      }
    >
      <div className="text-xs text-text-muted mb-3">
        The ledger of evaluated calls in shadow and enforce mode, newest first. Nothing is written while the mode is
        off.
      </div>
      {rows === null && !error && (
        <LoadingSkeleton label="Loading decisions">
          <div className="space-y-3">
            <SkeletonCard />
            <SkeletonCard />
          </div>
        </LoadingSkeleton>
      )}
      {!!error && <ListState kind="error" things="decisions" error={error} onRetry={() => setReads((n) => n + 1)} />}
      {rows !== null && !error && rows.length === 0 && (
        <div className="text-xs text-text-muted py-4 text-center">
          No decisions{filter ? ` with decision=${filter}` : ""} yet.
        </div>
      )}
      {rows !== null && !error && rows.length > 0 && (
        <div aria-busy={refreshing || undefined}>
          <Table
            size="sm"
            headers={[
              { label: "When", width: "110px" },
              { label: "Agent" },
              { label: "Operation" },
              { label: "Decision", width: "96px" },
              { label: "Enforced", width: "84px" },
              { label: "Determining rules" },
              { label: "Errors" },
              { label: "Latency", width: "84px" },
            ]}
            rows={rows.map((r, i) => ({
              id: `${r.request_id || r.ts}-${i}`,
              items: [
                {
                  // ListTime is the time and its UTC tooltip: no second one around it.
                  label: <ListTime time={r.ts} quiet className="text-text-muted" />,
                },
                { label: <AgentBadge agent={r.agent} /> },
                { label: <span className="font-mono text-xs">{r.operation}</span> },
                { label: <DecisionChip decision={r.decision} /> },
                { label: <Bool v={r.enforced} /> },
                {
                  label: r.determining_rules.length ? (
                    <span className="font-mono text-xs">{r.determining_rules.join(", ")}</span>
                  ) : (
                    DASH
                  ),
                  overflowMode: "wrap",
                },
                {
                  label: r.errors.length ? <span className="text-xs text-danger">{r.errors.join("; ")}</span> : DASH,
                  overflowMode: "wrap",
                },
                { label: <span className="tabular-nums">{r.latency_ms} ms</span> },
              ],
            }))}
          />
        </div>
      )}
    </Card>
  );
}
