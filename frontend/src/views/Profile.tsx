import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Plus, Trash2, Edit3, X, Check, Copy, Bot, User, Eye, EyeOff } from "lucide-react";
import {
  listProfile,
  setProfileSection,
  deleteProfileSection,
  getProfileActivity,
  type ProfileEntry,
  type ProfileActivityEntry,
  type Identity,
} from "../api";
import { useStore, useIdentity } from "../lib/state";
import { Skeleton } from "../components/Skeleton";
import { fmtInt, humanizeError, relativeTime } from "../lib/format";
import { useVisiblePoll } from "../lib/hooks";
import { toast } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { IconButton } from "../components/IconButton";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { pluginInstallLines } from "../lib/clients";

const DEFAULT_CHAR_LIMIT = 8192;

const TEMPLATES: Array<{
  label: string;
  title: string;
  description: string;
  value: string;
}> = [
  {
    label: "persona",
    title: "Terse senior engineer",
    description: "Direct, technical, pushes back.",
    value:
      "Helpful, terse — 3 sentences max unless I ask for depth.\n" +
      'No preamble ("Sure!", "Great question"). No closing wrap-ups.\n' +
      "Push back when I'm wrong. Don't agree by default.\n" +
      "Use code, file:line refs, command examples.",
  },
  {
    label: "persona",
    title: "Patient pair programmer",
    description: "Explains as it goes.",
    value:
      "Walk me through your reasoning before showing code.\n" +
      "Explain trade-offs of the approach you chose.\n" +
      "Catch me when I'm about to make a mistake; don't just answer\n" +
      "the literal question if I'm asking the wrong one.",
  },
  {
    label: "persona",
    title: "Plain-language explainer",
    description: "Minimal jargon.",
    value:
      "Use plain English. Define jargon the first time it appears.\n" +
      "Lead with the bottom line; details after.\n" +
      "Draw an analogy before going deep on anything complex.",
  },
];

export function Profile() {
  const identity = useIdentity();
  const [entries, setEntries] = useState<ProfileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [newSeed, setNewSeed] = useState<{ label: string; value: string } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);

  function toggleSelect(label: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }

  async function bulkDelete() {
    if (selected.size === 0 || bulkBusy) return;
    const ok = await confirmAction({
      title: `Delete ${selected.size} profile section${selected.size === 1 ? "" : "s"}?`,
      message: "Soft-deleted with a 14-day grace window — restorable from the Memories view.",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    setBulkBusy(true);
    try {
      await Promise.all(Array.from(selected).map((label) => deleteProfileSection(identity, label)));
      toast(`Deleted ${selected.size} section${selected.size === 1 ? "" : "s"}`, "ok");
      setSelected(new Set());
      refresh();
    } catch (e) {
      toast(humanizeError(e), "error");
    } finally {
      setBulkBusy(false);
    }
  }

  const filteredEntries = entries
    ? entries.filter((e) => {
        const q = query.trim().toLowerCase();
        if (!q) return true;
        return e.label.toLowerCase().includes(q) || e.value.toLowerCase().includes(q);
      })
    : null;

  // The scope a refresh was asked for; one that answers after the namespace or agent moved
  // on is dropped, so namespace A's sections never render under B.
  const scope = `${identity.org}|${identity.namespace}|${identity.agent}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  async function refresh() {
    setError(null);
    try {
      const out = await listProfile(identity);
      if (scopeRef.current !== scope) return;
      setEntries(out.entries);
    } catch (e) {
      if (scopeRef.current !== scope) return;
      setError(humanizeError(e));
    }
  }

  // Stable callbacks keyed by label, so a memoized SectionRow re-renders only when its
  // section changes — not on each of the page's eighteen states settling (42 ms at 4x,
  // the top function on boot).
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const editSection = useCallback((label: string) => setEditing(label), []);
  const deleteSection = useCallback(
    (label: string) => {
      void onDelete(identity, label, () => refreshRef.current());
    },
    [identity],
  );
  useEffect(() => {
    setEntries(null);
    void refreshRef.current();
  }, [identity.org, identity.namespace, identity.agent]);

  const pendingEdit = useStore((s) => s.pendingEditProfileLabel);
  const requestEditProfile = useStore((s) => s.requestEditProfile);
  useEffect(() => {
    if (!pendingEdit || !entries) return;
    if (entries.some((e) => e.label === pendingEdit)) {
      setEditing(pendingEdit);
    }
    requestEditProfile(null);
  }, [pendingEdit, entries, requestEditProfile]);

  function openNew(seed: { label: string; value: string } | null) {
    setNewSeed(seed);
    setNewOpen(true);
  }

  if (error) {
    return (
      <PageContainer>
        <Header onNew={() => openNew(null)} entries={[]} />
        <p className="text-base text-[var(--color-danger)]">Couldn't load profile: {error}</p>
      </PageContainer>
    );
  }

  if (entries === null) {
    return (
      <PageContainer>
        <Header onNew={() => openNew(null)} entries={[]} />
        <div className="space-y-3 mt-6">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      </PageContainer>
    );
  }

  const isEmpty = entries.length === 0 && !newOpen;

  return (
    <PageContainer>
      <Header onNew={() => openNew(null)} entries={entries} empty={entries.length === 0} />

      {isEmpty ? (
        <EmptyState onPickTemplate={(t) => openNew({ label: t.label, value: t.value })} />
      ) : (
        <div className="space-y-3 mt-6">
          <input
            aria-label="Search profile sections"
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search sections by label or value…"
            className="w-full px-3 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]"
          />
          {selected.size > 0 && (
            <div className="sticky top-0 z-[2] animate-fade-in motion-reduce:animate-none! flex items-center gap-2 p-2.5 rounded-xl border border-[var(--color-accent)] bg-[var(--color-accent-soft)] shadow-sm">
              <span className="text-sm font-medium text-[var(--color-accent)] tabular">{selected.size} selected</span>
              <div className="flex-1" />
              <button
                type="button"
                onClick={bulkDelete}
                disabled={bulkBusy}
                className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-md text-danger bg-danger/15 hover:bg-danger/25 disabled:opacity-50"
              >
                <Trash2 className="w-3 h-3" /> Delete all
              </button>
              <button
                type="button"
                onClick={() => setSelected(new Set())}
                className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-md text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)]"
              >
                <X className="w-3 h-3" /> Clear
              </button>
            </div>
          )}
          {newOpen && (
            <SectionEditor
              label={newSeed?.label ?? ""}
              value={newSeed?.value ?? ""}
              isNew
              onCancel={() => {
                setNewOpen(false);
                setNewSeed(null);
              }}
              onSaved={() => {
                setNewOpen(false);
                setNewSeed(null);
                refresh();
              }}
            />
          )}
          {filteredEntries && filteredEntries.length === 0 && query && (
            <div className="text-center py-8 text-sm text-[var(--color-text-muted)]">
              No sections matching <span className="font-mono">{query}</span>.
            </div>
          )}
          {(filteredEntries ?? entries).map((b) =>
            editing === b.label ? (
              <SectionEditor
                key={b.label}
                label={b.label}
                value={b.value}
                isNew={false}
                onCancel={() => setEditing(null)}
                onSaved={() => {
                  setEditing(null);
                  refresh();
                }}
              />
            ) : (
              <div
                key={b.label}
                onClick={
                  selected.size > 0 ? () => toggleSelect(b.label) : () => useStore.getState().inspectProfile(b.label)
                }
                className={
                  "p-4 rounded-lg border bg-[var(--color-surface)] flex items-start gap-3 transition-colors cursor-pointer " +
                  (selected.has(b.label)
                    ? "border-[var(--color-accent)] ring-1 ring-[var(--color-accent)]/40"
                    : "border-[var(--color-border)] hover:border-[var(--color-border-strong)]")
                }
              >
                <input
                  type="checkbox"
                  checked={selected.has(b.label)}
                  onChange={() => toggleSelect(b.label)}
                  onClick={(e) => e.stopPropagation()}
                  aria-label={`Select section ${b.label}`}
                  className="mt-1 shrink-0 cursor-pointer"
                />
                <div className="flex-1 min-w-0">
                  <SectionRow embedded section={b} onEdit={editSection} onDelete={deleteSection} />
                </div>
              </div>
            ),
          )}
        </div>
      )}

      <ReachSection />
    </PageContainer>
  );
}

function Header({ onNew, entries, empty }: { onNew: () => void; entries: ProfileEntry[]; empty?: boolean }) {
  const [previewOpen, setPreviewOpen] = useState(false);
  const namespace = useStore((s) => s.identity?.namespace ?? "");

  return (
    <>
      <div className="flex items-start justify-between gap-3 mb-1">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Your AI profile</h1>
          <div className="mt-1">
            <ScopeBadge kind="namespace" value={namespace} />
          </div>
          <p className="text-sm text-[var(--color-text-muted)] mt-1">
            One profile. Every AI you use reads it before responding.
          </p>
        </div>
        <button
          type="button"
          onClick={onNew}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)]"
        >
          <Plus className="w-4 h-4" />
          New section
        </button>
      </div>

      <div className="flex items-center gap-4 mt-4 text-xs">
        <ActivityChip empty={empty} />
        {entries.length > 0 && (
          <button
            type="button"
            onClick={() => setPreviewOpen((v) => !v)}
            className="inline-flex items-center gap-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
          >
            {previewOpen ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            {previewOpen ? "Hide preview" : "Preview rendered"}
          </button>
        )}
      </div>

      {previewOpen && entries.length > 0 && <Preview entries={entries} onClose={() => setPreviewOpen(false)} />}
    </>
  );
}

/** `empty` is true once the profile has loaded with no sections: a client still fetches it
 *  at session start, but gets nothing, so the chip must not say it was "read". */
function ActivityChip({ empty }: { empty?: boolean }) {
  const identity = useIdentity();
  const [entries, setEntries] = useState<ProfileActivityEntry[] | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [expanded, setExpanded] = useState(false);

  // As on the page: an answer for a namespace the chip has left is dropped.
  const scope = `${identity.org}|${identity.namespace}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  async function refresh() {
    try {
      const out = await getProfileActivity(identity, 7);
      if (scopeRef.current !== scope) return;
      setEntries(out.entries);
      setUnavailable(false);
    } catch {
      if (scopeRef.current !== scope) return;
      setUnavailable(true);
    }
  }

  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  useEffect(() => {
    void refreshRef.current();
  }, [identity.org, identity.namespace]);
  // Sleeps while the tab is hidden; fires once on return.
  useVisiblePoll(refresh, 60_000, !unavailable);

  if (unavailable || entries === null) return null;

  if (entries.length === 0) {
    return (
      <span
        className="inline-flex items-center gap-1.5 text-[var(--color-text-muted)]"
        title="Counts every way a client reads the profile: a plugin session start, an MCP connect, a typed “use loom”, or the SDK. Nothing in the last 7 days."
      >
        <span className="inline-block w-2 h-2 rounded-full bg-[var(--color-text-muted)] opacity-50" />
        No AI client has read this in the last 7 days
      </span>
    );
  }

  const mostRecent = entries[0];
  const ms = mostRecent.last_activated_at ? Date.now() - new Date(mostRecent.last_activated_at).getTime() : Infinity;
  const dotClass =
    ms < 3_600_000 ? "bg-success" : ms < 86_400_000 ? "bg-[var(--color-warn)]" : "bg-[var(--color-text-muted)]";

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        title={
          empty
            ? "These clients fetch the profile at session start or MCP connect. It has no sections, so they got nothing: add one to give them something."
            : undefined
        }
        className="inline-flex items-center gap-1.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
      >
        <span
          className={`inline-block w-2 h-2 rounded-full ${empty ? "bg-[var(--color-text-muted)] opacity-50" : dotClass}`}
        />
        {empty ? "Checked by" : "Read by"} {entries.length === 1 ? mostRecent.agent : `${entries.length} clients`} ·{" "}
        {mostRecent.last_activated_at ? relativeTime(mostRecent.last_activated_at) : "—"}
        {empty && " · empty, nothing to read yet"}
      </button>
      {expanded && (
        <div className="absolute left-0 top-6 z-10 min-w-[280px] rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] shadow-lg p-2 space-y-1">
          {entries.map((e) => (
            <div key={e.agent} className="flex items-center justify-between gap-3 px-2 py-1">
              <span className="min-w-0">
                <span className="font-mono text-xs">{e.agent}</span>
                {e.operations.length > 0 && (
                  <span className="block text-2xs text-[var(--color-text-subtle)]">
                    via {e.operations.map(readPathLabel).join(", ")}
                  </span>
                )}
              </span>
              <span className="shrink-0 text-2xs text-[var(--color-text-muted)] tabular-nums">
                {e.event_count}× · {relativeTime(e.last_activated_at)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** How a client read the profile, in the words the page uses for that path. An op this
 * build does not know shows under its own name. */
export function readPathLabel(op: string): string {
  switch (op) {
    case "context.pack":
      return "plugin session start";
    case "mcp.initialize":
      return "MCP connect";
    case "loom_activate":
      return "“use loom”";
    case "profile.render":
      return "SDK";
    default:
      return op;
  }
}

function Preview({ entries, onClose }: { entries: ProfileEntry[]; onClose: () => void }) {
  const [format, setFormat] = useState<"xml" | "markdown">("xml");

  const rendered = useMemo(() => {
    const sorted = [...entries].sort((a, b) => a.label.localeCompare(b.label));
    if (format === "markdown") {
      return sorted.map((b) => `## ${b.label}\n${b.value}`).join("\n\n");
    }
    return sorted.map((b) => `<section label="${b.label}">${b.value}</section>`).join("\n");
  }, [entries, format]);

  return (
    <section className="mt-4 rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] p-3">
      <div className="flex items-center gap-2 mb-2">
        <div className="inline-flex rounded border border-[var(--color-border)] overflow-hidden text-2xs">
          {(["xml", "markdown"] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFormat(f)}
              className={
                "px-2 py-0.5 " +
                (format === f
                  ? "bg-[var(--color-accent)] text-[var(--color-on-accent)]"
                  : "hover:bg-[var(--color-surface)] text-[var(--color-text-muted)]")
              }
            >
              {f}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => {
            navigator.clipboard.writeText(rendered);
            toast("Copied", "ok");
          }}
          className="inline-flex items-center gap-1 px-2 py-0.5 text-2xs rounded border border-[var(--color-border)] hover:bg-[var(--color-surface)]"
        >
          <Copy className="w-3 h-3" />
          Copy
        </button>
        <span className="ml-auto text-2xs text-[var(--color-text-muted)] tabular-nums">
          {fmtInt(rendered.length)} chars
        </span>
        <button
          type="button"
          onClick={onClose}
          className="p-0.5 rounded hover:bg-[var(--color-surface)] text-[var(--color-text-muted)]"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      <pre className="text-xs font-mono whitespace-pre-wrap break-words p-2 rounded bg-[var(--color-surface)] max-h-56 overflow-y-auto">
        {rendered}
      </pre>
    </section>
  );
}

// Where the profile goes, by the path that actually carries it. The Claude Code and Codex
// plugins inject it at session start through context.pack; every MCP client receives it
// in the connect handshake's instructions; "use loom" only re-delivers what the client
// already has, so it is the fallback, not the headline. The bot snippet is a developer's
// business and folds shut — most people on this page are not writing a bot.
export const BOT_SNIPPET = `from loom import Agent
from anthropic import AsyncAnthropic

agent = Agent(org="...", namespace="...", agent="<your-bot>")  # reads LOOM_API_KEY
claude = AsyncAnthropic()

# Once, when the conversation starts:
system = await agent.profile.system_prompt(base="You are my bot.")

# Every turn reuses \`system\` — no re-fetch:
async def turn(user_msg, history):
    resp = await claude.messages.create(
        model="claude-opus-5",  # pin whatever Opus is current
        system=system,
        messages=history + [{"role": "user", "content": user_msg}],
    )
    # content may include tool_use / thinking blocks — filter to text.
    return "".join(b.text for b in resp.content if b.type == "text")`;

function ReachSection() {
  const plugin = pluginInstallLines("claude-code") ?? "";
  return (
    <section className="mt-8 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]">
      <div className="px-4 py-3 border-b border-[var(--color-border)]">
        <span className="text-sm font-medium">Where this profile goes</span>
        <p className="mt-0.5 text-xs text-[var(--color-text-muted)]">
          Every client reads it at the start of a conversation. The chip above says which ones did.
        </p>
      </div>
      <div className="px-4 py-3 space-y-4 text-sm text-[var(--color-text)] leading-relaxed">
        <div>
          <div className="font-medium text-xs uppercase tracking-wide text-[var(--color-text-muted)] mb-1">
            Claude Code · Codex — with the Loom plugin
          </div>
          <div className="mb-2">Delivered when a session starts, with your relevant memories. Nothing to type.</div>
          <details className="text-xs text-[var(--color-text-muted)]">
            <summary className="cursor-pointer hover:text-[var(--color-text)]">
              Not installed yet? Two commands.
            </summary>
            <pre className="mt-1.5 text-2xs font-mono whitespace-pre-wrap break-words p-2 rounded bg-[var(--color-surface-2)]">
              {plugin}
            </pre>
          </details>
        </div>
        <div>
          <div className="font-medium text-xs uppercase tracking-wide text-[var(--color-text-muted)] mb-1">
            Claude Desktop · Cursor · any MCP client
          </div>
          <div className="mb-1">Delivered when the client connects, as Loom&apos;s session instructions.</div>
          <div className="text-xs text-[var(--color-text-muted)] leading-relaxed">
            If a client ignores server instructions, type{" "}
            <code className="font-mono bg-[var(--color-accent-soft)] text-[var(--color-accent)] rounded px-1 py-0.5">
              use loom
            </code>{" "}
            once at the start of the conversation; it re-delivers the same text.
          </div>
        </div>
        <details className="group">
          <summary className="cursor-pointer font-medium text-xs uppercase tracking-wide text-[var(--color-text-muted)] hover:text-[var(--color-text)]">
            Your own bot
          </summary>
          <div className="mt-2 space-y-3">
            <div>
              Cache the profile once per conversation, pass it as <code>system=</code> on every LLM call:
            </div>
            <div className="relative">
              <button
                type="button"
                onClick={() => {
                  navigator.clipboard.writeText(BOT_SNIPPET);
                  toast("Copied", "ok");
                }}
                className="absolute top-1.5 right-1.5 inline-flex items-center gap-1 px-1.5 py-0.5 text-2xs rounded border border-[var(--color-border)] bg-[var(--color-surface)] hover:bg-[var(--color-surface-2)]"
              >
                <Copy className="w-3 h-3" />
                Copy
              </button>
              <pre className="text-xs font-mono whitespace-pre-wrap break-words p-2.5 pr-12 rounded bg-[var(--color-surface-2)] overflow-x-auto">
                {BOT_SNIPPET}
              </pre>
            </div>
            <div className="text-xs text-[var(--color-text-muted)] leading-relaxed">
              TypeScript: <code>loom.profile.render(&#123; format: &quot;xml&quot; &#125;)</code> returns the same text
              to prepend yourself.
            </div>
            <div className="text-xs text-[var(--color-text-muted)] leading-relaxed">
              <span className="text-[var(--color-text)] font-medium">Building a product with multiple end-users?</span>{" "}
              Pass <code>user_id=&quot;alice@example.com&quot;</code> on every Loom call so each user gets their own
              profile + facts, scoped to that <code>user_id</code>. If you cache the rendered <code>system</code>{" "}
              prompt, key the cache by <code>user_id</code> too — sharing one cached <code>system</code> across users
              leaks personal facts between sessions.
            </div>
          </div>
        </details>
      </div>
    </section>
  );
}

function EmptyState({ onPickTemplate }: { onPickTemplate: (t: (typeof TEMPLATES)[number]) => void }) {
  return (
    <div className="mt-6 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-6">
      <div className="text-center mb-5">
        <h2 className="text-xl font-medium mb-1">Start with a persona</h2>
        <p className="text-sm text-[var(--color-text-muted)]">
          Pick the style you want every AI conversation to default to.
        </p>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {TEMPLATES.map((t) => (
          <button
            key={t.title}
            type="button"
            onClick={() => onPickTemplate(t)}
            className="text-left rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] hover:border-[var(--color-accent)] hover:bg-[var(--color-surface)] p-3 transition-colors"
          >
            <div className="text-sm font-semibold mb-1">{t.title}</div>
            <div className="text-xs text-[var(--color-text-muted)] mb-2">{t.description}</div>
            <pre className="text-2xs font-mono text-[var(--color-text-muted)] whitespace-pre-wrap leading-relaxed line-clamp-3">
              {t.value}
            </pre>
          </button>
        ))}
      </div>
    </div>
  );
}

const SectionRow = memo(function SectionRow({
  section,
  onEdit,
  onDelete,
  embedded = false,
}: {
  section: ProfileEntry;
  onEdit: (label: string) => void;
  onDelete: (label: string) => void;
  /** When true, the row drops its own border + padding so a parent
   *  wrapper can host the card chrome — used by the Profile bulk-select
   *  layout where the checkbox lives inside the same border as content. */
  embedded?: boolean;
}) {
  const valueLen = section.value.length;
  const cap = section.char_limit ?? DEFAULT_CHAR_LIMIT;
  const pct = Math.min(100, (valueLen / cap) * 100);
  const headroomWarn = pct >= 95;
  const editor = section.last_edited_by || "—";
  const isAgentEdit = editor !== "loom-ui" && editor !== "-";
  const lines = section.value.split("\n");
  const truncated = lines.length > 6;
  const [expanded, setExpanded] = useState(false);

  return (
    <div className={embedded ? "" : "rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4"}>
      <div className="flex items-start justify-between mb-2 gap-3">
        <div className="flex items-baseline gap-2 min-w-0 flex-1">
          <div className="font-mono text-base font-semibold truncate">{section.label}</div>
          <div className="text-2xs font-mono tabular-nums text-[var(--color-text-muted)] whitespace-nowrap">
            {fmtInt(valueLen)} / {fmtInt(cap)}
          </div>
        </div>
        <div className="flex items-center gap-1">
          <IconButton
            onClick={(e) => {
              e.stopPropagation();
              onEdit(section.label);
            }}
            label={`Edit ${section.label}`}
            tooltip="Edit"
            className="hover:bg-[var(--color-surface-2)] text-[var(--color-text-muted)]"
          >
            <Edit3 className="w-4 h-4" />
          </IconButton>
          <IconButton
            onClick={(e) => {
              e.stopPropagation();
              onDelete(section.label);
            }}
            label={`Delete ${section.label}`}
            tooltip="Delete"
            className="hover:bg-[var(--color-surface-2)] text-[var(--color-text-muted)] hover:text-[var(--color-danger)]"
          >
            <Trash2 className="w-4 h-4" />
          </IconButton>
        </div>
      </div>
      <pre className="text-sm whitespace-pre-wrap break-words mb-2 font-sans text-[var(--color-text)]">
        {expanded || !truncated ? section.value : lines.slice(0, 6).join("\n") + "\n…"}
      </pre>
      {truncated && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
          className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-accent)] mb-2"
        >
          {expanded ? "Show less" : `Show all (${lines.length} lines)`}
        </button>
      )}
      <div className="flex items-center gap-3 text-xs text-[var(--color-text-muted)] mt-2">
        <div className="flex-1 h-1.5 rounded-full bg-[var(--color-surface-2)] overflow-hidden">
          <div
            className={"h-full " + (headroomWarn ? "bg-[var(--color-warn)]" : "bg-[var(--color-accent)]")}
            style={{ width: `${pct}%` }}
          />
        </div>
        <div className="flex items-center gap-1.5 whitespace-nowrap">
          {isAgentEdit ? <Bot className="w-3.5 h-3.5" /> : <User className="w-3.5 h-3.5" />}
          <span className="font-mono">{editor}</span>
          {section.updated_at && <span>· {relativeTime(section.updated_at)}</span>}
        </div>
      </div>
    </div>
  );
});

function SectionEditor({
  label: initialLabel,
  value: initialValue,
  isNew,
  onCancel,
  onSaved,
}: {
  label: string;
  value: string;
  isNew: boolean;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const identity = useIdentity();
  const [label, setLabel] = useState(initialLabel);
  const [value, setValue] = useState(initialValue);
  const [saving, setSaving] = useState(false);

  const measuredLen = JSON.stringify(value).length;
  const over = measuredLen > DEFAULT_CHAR_LIMIT;
  const pct = Math.min(100, (measuredLen / DEFAULT_CHAR_LIMIT) * 100);

  async function onSave() {
    if (!label.trim()) {
      toast("Label is required", "error");
      return;
    }
    if (over) {
      toast(`Value is ${measuredLen} chars; cap is ${DEFAULT_CHAR_LIMIT}`, "error");
      return;
    }
    setSaving(true);
    try {
      await setProfileSection(identity, label.trim(), value, DEFAULT_CHAR_LIMIT);
      toast(isNew ? "Section created" : "Section saved", "ok");
      onSaved();
    } catch (e) {
      toast(humanizeError(e), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-lg border border-[var(--color-accent)] bg-[var(--color-surface)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <input
          aria-label="Section label"
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="section name (e.g. persona, about-me)"
          disabled={!isNew}
          className="font-mono text-base font-semibold bg-transparent border-b border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none disabled:opacity-60 flex-1"
        />
        <div className="flex items-center gap-1 ml-3">
          <IconButton
            onClick={onCancel}
            disabled={saving}
            label="Cancel"
            className="hover:bg-[var(--color-surface-2)] text-[var(--color-text-muted)]"
          >
            <X className="w-4 h-4" />
          </IconButton>
          <IconButton
            onClick={onSave}
            disabled={saving || over}
            label="Save"
            className="hover:bg-[var(--color-surface-2)] text-[var(--color-accent)] disabled:opacity-40"
          >
            <Check className="w-4 h-4" />
          </IconButton>
        </div>
      </div>
      <textarea
        aria-label="Section text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        rows={Math.min(20, Math.max(4, value.split("\n").length + 1))}
        placeholder="This is what your AI reads, exactly as typed."
        className="w-full text-sm font-mono bg-[var(--color-surface-2)] rounded-md p-3 outline-none focus:ring-2 focus:ring-[var(--color-accent)] resize-y"
      />
      <div className="flex items-center gap-3 text-xs">
        <div className="flex-1 h-1.5 rounded-full bg-[var(--color-surface-2)] overflow-hidden">
          <div
            className={"h-full " + (over ? "bg-[var(--color-danger)]" : "bg-[var(--color-accent)]")}
            style={{ width: `${pct}%` }}
          />
        </div>
        <div
          className={
            "font-mono tabular-nums whitespace-nowrap " +
            (over ? "text-[var(--color-danger)]" : "text-[var(--color-text-muted)]")
          }
        >
          {fmtInt(measuredLen)} / {fmtInt(DEFAULT_CHAR_LIMIT)}
        </div>
      </div>
    </div>
  );
}

async function onDelete(identity: Identity, label: string, refresh: () => void) {
  const ok = await confirmAction({
    title: `Delete section "${label}"?`,
    message: "Soft-deleted with a 14-day grace window — restorable from the Memories view.",
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    await deleteProfileSection(identity, label);
    toast("Section deleted", "ok");
    refresh();
  } catch (e) {
    toast(humanizeError(e), "error");
  }
}
