import { useState } from "react";
import { Gavel, Plus, X as XIcon, Play, CheckCircle2, XCircle, Trash2 } from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";
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
import { Skeleton, SkeletonCard } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { displayAgent, humanizeError, relativeTime } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { PageContainer } from "../../layout/PageContainer";
import { oneOf } from "../../lib/oneOf";
import { isRecord } from "../../lib/guards";

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

const MODE_META: Record<PolicyMode, { cls: string; blurb: string }> = {
  off: {
    cls: "text-[var(--color-text-muted)] border-[var(--color-border)]",
    blurb: "Rules are stored but never evaluated. Nothing is logged or blocked.",
  },
  shadow: {
    cls: "text-warn border-warn/40",
    blurb: "Every matching call is evaluated and recorded below, but a deny does not block the call.",
  },
  enforce: {
    cls: "text-danger border-danger/40",
    blurb: "A deny blocks the call with 403 POLICY_FORBIDDEN. Rule errors on write-shaped calls also deny.",
  },
};

const MODES: readonly PolicyMode[] = ["off", "shadow", "enforce"];

/** Badge style and blurb for a server-reported mode; an unknown mode reads as off. */
function modeMeta(mode: string) {
  return MODE_META[oneOf(MODES, mode) ?? "off"];
}

function ModeBadge({ mode }: { mode: string }) {
  const meta = modeMeta(mode);
  return (
    <span className={`inline-flex items-center gap-1 text-2xs px-2 py-0.5 rounded-full border font-medium ${meta.cls}`}>
      mode: {mode}
    </span>
  );
}

function DecisionChip({ decision }: { decision: string }) {
  const deny = decision === "deny";
  return (
    <span
      className={`inline-flex items-center gap-1 text-2xs px-2 py-0.5 rounded-full border ${deny ? "text-danger border-danger/40" : "text-success border-success/40"}`}
    >
      {deny ? <XCircle size={12} /> : <CheckCircle2 size={12} />} {decision}
    </span>
  );
}

function Bool({ v }: { v: boolean | null | undefined }) {
  if (v === null || v === undefined) return <span className="text-[var(--color-text-subtle)]">—</span>;
  return <span className={v ? "text-success" : "text-[var(--color-text-muted)]"}>{v ? "yes" : "no"}</span>;
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

const INPUT_CLS =
  "w-full px-3 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]";
const TEXTAREA_CLS = `${INPUT_CLS} font-mono text-xs resize-y`;
const LABEL_CLS = "block text-2xs uppercase tracking-wide text-[var(--color-text-muted)] mb-1";
const PRIMARY_BTN =
  "inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-50";
const SECONDARY_BTN =
  "inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium border border-[var(--color-border-strong)] rounded-md hover:bg-[var(--color-surface-2)] disabled:opacity-50";
const TH_CLS = "text-left px-3 py-2 font-medium";

function Card({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)]">{title}</div>
        {action}
      </div>
      {children}
    </div>
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
  const [mode, setMode] = useState<PolicyMode | string>("off");
  const [error, setError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [decisionsVersion, setDecisionsVersion] = useState(0);

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    setRules(null);
    setError(null);
    listPolicies({ org: identity.org, namespace: identity.namespace }, { signal })
      .then((r) => {
        if (live()) {
          setRules(r.rules || []);
          setMode(r.mode || "off");
        }
      })
      .catch((e) => {
        if (live()) {
          setError(humanizeError(e));
          setRules([]);
        }
      });
  };
  useAbortableEffect(load, [identity.org, identity.namespace]);

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
      load();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    }
  };

  const meta = modeMeta(mode);

  return (
    <PageContainer>
      <div className="flex items-baseline gap-3 mb-2 flex-wrap">
        <h1 className="text-3xl font-semibold tracking-tight inline-flex items-center gap-2">
          <Gavel size={20} /> Policies
        </h1>
        <ScopeBadge kind="namespace" value={identity.namespace} />
        <ModeBadge mode={mode} />
      </div>
      <p className="text-sm text-[var(--color-text-muted)] mb-5 max-w-[80ch]">
        Rules over an agent's own recent history: forbid or require an action based on what the same caller did in the
        last minutes or hours. <span className="text-[var(--color-text)]">{meta.blurb}</span> The mode is the server's{" "}
        <code className="font-mono text-xs">LOOM_POLICY_MODE</code>; it cannot be changed here.
      </p>

      <div className="space-y-4">
        <Card
          title="Rules"
          action={
            <button onClick={() => setAddOpen(true)} className={PRIMARY_BTN}>
              <Plus size={12} /> Add rule
            </button>
          }
        >
          {rules === null && <Skeleton className="h-24 w-full" />}
          {rules !== null && error && <div className="text-sm text-danger">{error}</div>}
          {rules !== null && !error && rules.length === 0 && (
            <EmptyState
              icon={Gavel}
              title="No rules"
              description="Add a rule to forbid or require an action based on the caller's recent history. Rules only take effect once the server mode leaves off."
            />
          )}
          {rules !== null && rules.length > 0 && (
            <div className="rounded-lg border border-[var(--color-border)] overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-[var(--color-surface-2)] text-2xs uppercase tracking-wide text-[var(--color-text-muted)]">
                  <tr>
                    <th className={TH_CLS}>Policy</th>
                    <th className={TH_CLS}>Effect</th>
                    <th className={TH_CLS}>Actions</th>
                    <th className={TH_CLS}>Condition</th>
                    <th className={TH_CLS}>Enabled</th>
                    <th className={TH_CLS}>Description</th>
                    <th className={TH_CLS}></th>
                  </tr>
                </thead>
                <tbody>
                  {rules.map((r) => (
                    <tr
                      key={r.policy_id}
                      className="border-t border-[var(--color-border)] hover:bg-[var(--color-surface-2)]"
                    >
                      <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">
                        <div>{r.policy_id}</div>
                        {r.updated_at && (
                          <div className="text-2xs font-sans text-[var(--color-text-subtle)]">
                            updated {relativeTime(r.updated_at)}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap">
                        <span
                          className={`text-2xs px-2 py-0.5 rounded-full border ${r.effect === "forbid" ? "text-danger border-danger/40" : "text-[var(--color-accent)] border-[var(--color-accent)]/40"}`}
                        >
                          {r.effect}
                        </span>
                      </td>
                      <td className="px-3 py-2 font-mono text-xs">{r.actions.join(", ")}</td>
                      <td className="px-3 py-2 font-mono text-xs max-w-[40ch]">
                        <div className="truncate" title={JSON.stringify(r.condition, null, 2)}>
                          {summariseCondition(r.condition)}
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        <Bool v={r.enabled} />
                      </td>
                      <td className="px-3 py-2 text-[var(--color-text-muted)] max-w-[36ch]">
                        <div className="truncate" title={r.description}>
                          {r.description || <span className="text-[var(--color-text-subtle)]">—</span>}
                        </div>
                      </td>
                      <td className="px-3 py-2 text-right whitespace-nowrap">
                        <button
                          onClick={() => remove(r)}
                          className="p-1 text-[var(--color-danger)] hover:bg-[var(--color-surface-2)] rounded"
                          aria-label={`Delete ${r.policy_id}`}
                          title="Delete"
                        >
                          <Trash2 size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
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
          load();
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
    <Dialog.Root
      open={open}
      onOpenChange={(v) => {
        if (!v) {
          onClose();
          setErr(null);
        }
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(640px,94vw)] max-h-[90vh] flex flex-col rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
          <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-[var(--color-border)] shrink-0">
            <div>
              <Dialog.Title className="text-md font-semibold tracking-tight">Add rule</Dialog.Title>
              <Dialog.Description className="text-xs text-[var(--color-text-muted)] mt-1">
                Saving under an existing policy_id replaces that rule. Rules apply to every writer in this namespace.
              </Dialog.Description>
            </div>
            <Dialog.Close
              className="p-1.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              aria-label="Close"
            >
              <XIcon size={16} />
            </Dialog.Close>
          </div>
          <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-[1fr_140px] gap-3">
              <div>
                <label className={LABEL_CLS} htmlFor="policy-id">
                  policy_id
                </label>
                <input
                  id="policy-id"
                  value={policyId}
                  onChange={(e) => setPolicyId(e.target.value)}
                  placeholder="no-blind-delete"
                  className={`${INPUT_CLS} font-mono`}
                />
              </div>
              <div>
                <label className={LABEL_CLS} htmlFor="policy-effect">
                  effect
                </label>
                <select
                  id="policy-effect"
                  value={effect}
                  onChange={(e) => {
                    const v = oneOf(EFFECTS, e.target.value);
                    if (v !== null) setEffect(v);
                  }}
                  className={INPUT_CLS}
                >
                  {EFFECTS.map((x) => (
                    <option key={x} value={x}>
                      {x}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div>
              <label className={LABEL_CLS} htmlFor="policy-actions">
                actions (comma-separated operation names or globs)
              </label>
              <input
                id="policy-actions"
                value={actions}
                onChange={(e) => setActions(e.target.value)}
                placeholder="memory.delete, memory.*"
                className={`${INPUT_CLS} font-mono`}
              />
            </div>
            <div>
              <label className={LABEL_CLS} htmlFor="policy-description">
                description
              </label>
              <input
                id="policy-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Optional — why this rule exists"
                className={INPUT_CLS}
              />
            </div>
            <div>
              <label className={LABEL_CLS} htmlFor="policy-condition">
                condition (JSON)
              </label>
              <textarea
                id="policy-condition"
                value={condition}
                onChange={(e) => setCondition(e.target.value)}
                rows={8}
                spellCheck={false}
                className={TEXTAREA_CLS}
              />
              <div className="text-2xs text-[var(--color-text-muted)] mt-1.5 leading-relaxed">
                <span className="font-medium text-[var(--color-text)]">forbid</span> denies when the condition holds;{" "}
                <span className="font-medium text-[var(--color-text)]">require</span> denies when it does not.{" "}
                {GRAMMAR_HELP}
              </div>
            </div>
            {err && (
              <div className="text-xs text-danger whitespace-pre-wrap break-words rounded-md border border-danger/30 bg-danger/5 px-3 py-2">
                {err}
              </div>
            )}
          </div>
          <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-[var(--color-border)] shrink-0">
            <button
              onClick={() => {
                onClose();
                setErr(null);
              }}
              className="px-3 py-1.5 text-sm font-medium text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] rounded-md"
            >
              Cancel
            </button>
            <button onClick={submit} disabled={busy} className={PRIMARY_BTN}>
              <Plus size={12} /> {busy ? "Saving…" : "Save rule"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
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
      <div className="text-xs text-[var(--color-text-muted)] mb-3">
        Evaluate the stored rules against a hypothetical call, using the same evaluator and trace the gate reads.
        Nothing is blocked; <span className="font-mono">would_enforce</span> says what enforce mode would do.
      </div>
      <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_2fr] gap-3 items-start">
        <div>
          <label className={LABEL_CLS} htmlFor="dry-operation">
            operation
          </label>
          <input
            id="dry-operation"
            value={operation}
            onChange={(e) => setOperation(e.target.value)}
            placeholder="memory.delete"
            className={`${INPUT_CLS} font-mono`}
          />
        </div>
        <div>
          <label className={LABEL_CLS} htmlFor="dry-agent">
            agent
          </label>
          <input
            id="dry-agent"
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
            placeholder="- (none)"
            className={`${INPUT_CLS} font-mono`}
          />
        </div>
        <div>
          <label className={LABEL_CLS} htmlFor="dry-args">
            args (JSON)
          </label>
          <textarea
            id="dry-args"
            value={args}
            onChange={(e) => setArgs(e.target.value)}
            rows={3}
            spellCheck={false}
            className={TEXTAREA_CLS}
          />
        </div>
      </div>
      <div className="mt-3 flex items-center gap-3 flex-wrap">
        <button onClick={run} disabled={busy} className={SECONDARY_BTN}>
          <Play size={13} /> {busy ? "Checking…" : "Check"}
        </button>
        {err && <span className="text-xs text-danger break-words">{err}</span>}
      </div>

      {result && (
        <div className="mt-4 space-y-3">
          <div className="flex items-center gap-2 flex-wrap text-xs text-[var(--color-text-muted)]">
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
                  determined by{" "}
                  <span className="font-mono text-[var(--color-text)]">{result.determining_rules.join(", ")}</span>
                </span>
              </>
            )}
          </div>
          {result.errors.length > 0 && (
            <div className="text-xs text-danger rounded-md border border-danger/30 bg-danger/5 px-3 py-2 space-y-0.5">
              {result.errors.map((e, i) => (
                <div key={i} className="break-words">
                  {e}
                </div>
              ))}
            </div>
          )}
          {result.rules.length === 0 && (
            <div className="text-xs text-[var(--color-text-subtle)]">
              No rule matches <span className="font-mono">{operation.trim()}</span>.
            </div>
          )}
          {result.rules.length > 0 && (
            <div className="rounded-lg border border-[var(--color-border)] overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-[var(--color-surface-2)] text-2xs uppercase tracking-wide text-[var(--color-text-muted)]">
                  <tr>
                    <th className={TH_CLS}>Rule</th>
                    <th className={TH_CLS}>Effect</th>
                    <th className={TH_CLS}>Holds</th>
                    <th className={TH_CLS}>Denies</th>
                    <th className={TH_CLS}>Leaf op</th>
                    <th className={TH_CLS}>Leaf holds</th>
                    <th className={TH_CLS}>Value</th>
                  </tr>
                </thead>
                <tbody>
                  {result.rules.map((v) => {
                    const leaves = v.leaves.length ? v.leaves : [null];
                    return leaves.map((lf, i) => (
                      <tr key={`${v.policy_id}-${i}`} className="border-t border-[var(--color-border)]">
                        {i === 0 && (
                          <>
                            <td
                              rowSpan={leaves.length}
                              className="px-3 py-2 font-mono text-xs whitespace-nowrap align-top"
                            >
                              {v.policy_id}
                            </td>
                            <td rowSpan={leaves.length} className="px-3 py-2 align-top">
                              {v.effect}
                            </td>
                            <td rowSpan={leaves.length} className="px-3 py-2 align-top">
                              <Bool v={v.holds} />
                            </td>
                            <td rowSpan={leaves.length} className="px-3 py-2 align-top">
                              <Bool v={v.denies} />
                            </td>
                          </>
                        )}
                        <td className="px-3 py-2 font-mono text-xs">
                          {lf ? lf.op : <span className="text-[var(--color-text-subtle)]">—</span>}
                        </td>
                        <td className="px-3 py-2">
                          {lf ? (
                            lf.skipped ? (
                              <span className="text-[var(--color-text-subtle)]" title={lf.skipped}>
                                skipped
                              </span>
                            ) : lf.error ? (
                              <span className="text-danger" title={lf.error}>
                                error
                              </span>
                            ) : (
                              <Bool v={lf.holds} />
                            )
                          ) : (
                            <span className="text-[var(--color-text-subtle)]">—</span>
                          )}
                        </td>
                        <td className="px-3 py-2 font-mono text-xs max-w-[32ch]">
                          {lf ? (
                            <div className="truncate" title={lf.error || lf.skipped || undefined}>
                              {lf.error ||
                                lf.skipped ||
                                (lf.value === null || lf.value === undefined
                                  ? "—"
                                  : typeof lf.value === "string"
                                    ? lf.value
                                    : JSON.stringify(lf.value))}
                            </div>
                          ) : (
                            <span className="text-[var(--color-text-subtle)]">—</span>
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

function DecisionsPanel({ version }: { version: number }) {
  const identity = useIdentity();
  const [rows, setRows] = useState<PolicyDecisionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<DecisionFilter>("");

  useAbortableEffect(
    (signal, live) => {
      setRows(null);
      setError(null);
      listPolicyDecisions(
        { org: identity.org, namespace: identity.namespace },
        { decision: filter || undefined, limit: 100, signal },
      )
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e) => {
          if (live()) {
            setError(humanizeError(e));
            setRows([]);
          }
        });
    },
    [identity.org, identity.namespace, filter, version],
  );

  return (
    <Card
      title="Decisions"
      action={
        <div className="flex items-center gap-1.5">
          {DECISION_FILTERS.map((f) => (
            <button
              key={f || "all"}
              onClick={() => setFilter(f)}
              className={`text-xs px-2.5 py-1 rounded-full border ${filter === f ? "bg-[var(--color-text)] text-[var(--color-bg)] border-transparent" : "border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"}`}
            >
              {f || "all"}
            </button>
          ))}
        </div>
      }
    >
      <div className="text-xs text-[var(--color-text-muted)] mb-3">
        The ledger of evaluated calls in shadow and enforce mode, newest first. Nothing is written while the mode is
        off.
      </div>
      {rows === null && (
        <div className="space-y-3">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}
      {rows !== null && error && <div className="text-sm text-danger">{error}</div>}
      {rows !== null && !error && rows.length === 0 && (
        <div className="text-xs text-[var(--color-text-subtle)] py-4 text-center">
          No decisions{filter ? ` with decision=${filter}` : ""} yet.
        </div>
      )}
      {rows !== null && rows.length > 0 && (
        <div className="rounded-lg border border-[var(--color-border)] overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-[var(--color-surface-2)] text-2xs uppercase tracking-wide text-[var(--color-text-muted)]">
              <tr>
                <th className={TH_CLS}>When</th>
                <th className={TH_CLS}>Agent</th>
                <th className={TH_CLS}>Operation</th>
                <th className={TH_CLS}>Decision</th>
                <th className={TH_CLS}>Enforced</th>
                <th className={TH_CLS}>Determining rules</th>
                <th className={TH_CLS}>Errors</th>
                <th className="text-right px-3 py-2 font-medium">Latency</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr
                  key={`${r.request_id || r.ts}-${i}`}
                  className="border-t border-[var(--color-border)] hover:bg-[var(--color-surface-2)]"
                >
                  <td className="px-3 py-2 whitespace-nowrap text-[var(--color-text-muted)]" title={r.ts}>
                    {relativeTime(r.ts)}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap">{displayAgent(r.agent)}</td>
                  <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">{r.operation}</td>
                  <td className="px-3 py-2">
                    <DecisionChip decision={r.decision} />
                  </td>
                  <td className="px-3 py-2">
                    <Bool v={r.enforced} />
                  </td>
                  <td className="px-3 py-2 font-mono text-xs max-w-[28ch]">
                    <div className="truncate" title={r.determining_rules.join(", ")}>
                      {r.determining_rules.length ? (
                        r.determining_rules.join(", ")
                      ) : (
                        <span className="text-[var(--color-text-subtle)]">—</span>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-xs text-danger max-w-[32ch]">
                    <div className="truncate" title={r.errors.join("\n")}>
                      {r.errors.length ? (
                        r.errors.join("; ")
                      ) : (
                        <span className="text-[var(--color-text-subtle)]">—</span>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums whitespace-nowrap">{r.latency_ms} ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
