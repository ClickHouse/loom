import { useEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {
  X,
  Star,
  Trash2,
  Copy,
  History,
  Link2,
  GitBranch,
  Pencil,
  Save,
  ChevronDown,
  ChevronRight,
  Maximize2,
  Minimize2,
  AlertTriangle,
  FolderInput,
  ShieldCheck,
  ShieldAlert,
  ArrowRightLeft,
  CornerDownRight,
} from "lucide-react";
import type {
  CodeLink,
  Identity,
  MemoryDetail,
  MemoryHistoryEntry,
  RelatedMemoryRow,
  LineageStep,
  MemoryProvenance,
  MemoryMoveResult,
} from "../api";
import { sharedTagVocabulary } from "../lib/sharedReads";
import { readSession, writeSession } from "../lib/afterPaint";
import {
  getMemory,
  setMemory,
  pinMemory,
  unpinMemory,
  deleteMemory,
  verifyMemory,
  memoryHistory,
  memoryRelated,
  memoryLineage,
  conflictKeys,
  getProvenance,
  moveMemory,
  restoreMemory,
} from "../api";
import { useStore } from "../lib/state";
import { Waypoints } from "lucide-react";
import { graphSeedHref, onGraphLinkClick } from "../lib/graphLink";
import { AgentBadge } from "./AgentBadge";
import { SkeletonText } from "./Skeleton";
import { DiffView } from "./DiffView";
import { toast } from "./Toast";
import { confirmAction } from "./ConfirmDialog";
import { CopyToNamespaceDialog } from "../flows/CopyToNamespaceDialog";
import { relativeTime, formatValue, humanizeError, SENTINEL_AGENTS } from "../lib/format";
import { detectEncodedValue } from "../lib/encoded";
import { useNamespaceRole } from "../lib/namespaceRole";
import { canCall } from "../lib/canCall";
import { historyChange, previousVersion } from "../lib/historyChange";

type SectionId = "history" | "related" | "provenance" | "lineage";
const SECTIONS_KEY = "loom_inspector_sections_v1";
const CLOSED: Record<SectionId, boolean> = { history: false, related: false, provenance: false, lineage: false };

/** A secondary section of the drawer: a heading that opens, and reads, on demand. */
function Disclosure({
  id,
  title,
  icon,
  open,
  onToggle,
  count,
  children,
}: {
  id: SectionId;
  title: string;
  icon: React.ReactNode;
  open: boolean;
  onToggle: (id: SectionId) => void;
  count?: string | number;
  children: React.ReactNode;
}) {
  return (
    <section>
      <button
        type="button"
        onClick={() => onToggle(id)}
        aria-expanded={open}
        aria-controls={`loom-inspector-${id}`}
        className="w-full flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors mb-2"
      >
        {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        {icon} {title}
        {count !== undefined && <span className="normal-case tracking-normal font-normal">· {count}</span>}
      </button>
      {open && <div id={`loom-inspector-${id}`}>{children}</div>}
    </section>
  );
}

function SectionEmpty({ children }: { children: React.ReactNode }) {
  return <div className="text-xs text-[var(--color-text-subtle)]">{children}</div>;
}

interface InspectorProps {
  identity: Identity;
  memoryKey: string;
  onClose: () => void;
  onChanged: () => void;
}

/**
 * Right-edge drawer showing one memory: value, metadata, tags, provenance,
 * related rows, history and lineage, with edit / pin / verify / copy /
 * delete in a sticky footer.
 *
 * The shell is a Radix `Dialog`: portal, overlay, focus trap, Escape, body scroll lock;
 * focus returns to the card that opened it on close (captured here — a triggerless Radix
 * dialog restores nothing on its own). Focus lands on the drawer itself so the key is
 * announced and the `e` / `d` / `p` shortcuts work straight away; they are bound to the
 * dialog content — not `document` — so they only fire while the drawer holds focus and
 * never while a field is being typed in. The slide-in animates `transform`/`opacity` only
 * and is disabled under `prefers-reduced-motion`; the expand toggle swaps width
 * instantly.
 */

export function MemoryInspector({ identity, memoryKey, onClose, onChanged }: InspectorProps) {
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [history, setHistory] = useState<MemoryHistoryEntry[] | null>(null);
  const [related, setRelated] = useState<RelatedMemoryRow[] | null>(null);
  const [lineage, setLineage] = useState<LineageStep[] | null>(null);
  const [provenance, setProvenance] = useState<MemoryProvenance | null>(null);
  const [conflictCount, setConflictCount] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  // A Restore in flight for the memory on screen; reset when the drawer moves on.
  const [restoring, setRestoring] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [diffVersion, setDiffVersion] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [copyOpen, setCopyOpen] = useState(false);
  // The "Rename (keep redirect)" form — open, the draft destination key, whether the old
  // key stays readable, the last dry-run.
  const [renaming, setRenaming] = useState(false);
  const [namespaceTags, setNamespaceTags] = useState<string[]>([]);
  // The drawer reads what is open. History, related, provenance and lineage are
  // disclosures, closed by default; a section reads once when it is opened, and which
  // ones a person keeps open is remembered for the session so their next card opens the
  // same way.
  const [sections, setSections] = useState<Record<SectionId, boolean>>(
    () => readSession<Record<SectionId, boolean>>(SECTIONS_KEY, 7 * 24 * 3600_000) ?? { ...CLOSED },
  );
  const [done, setDone] = useState<Set<SectionId>>(() => new Set());
  const requested = useRef<Set<SectionId>>(new Set());
  // Bumped whenever the drawer moves to another memory or identity.
  const inspection = useRef(0);
  const toggleSection = (id: SectionId) =>
    setSections((cur) => {
      const next = { ...cur, [id]: !cur[id] };
      writeSession(SECTIONS_KEY, next);
      return next;
    });
  const contentRef = useRef<HTMLDivElement | null>(null);
  // The element that opened the drawer (the card, usually), captured on the
  // first render — before Radix moves focus in — and refocused on close. A
  // Radix modal Dialog otherwise restores to its Trigger, which this
  // drawer has none of, so focus would fall to <body>.
  const [opener] = useState<HTMLElement | null>(() => {
    if (typeof document === "undefined") return null;
    const el = document.activeElement;
    return el instanceof HTMLElement ? el : null;
  });
  const inspect = useStore((s) => s.inspect);
  const searchByTag = useStore((s) => s.searchByTag);
  const openConversationThread = useStore((s) => s.openConversationThread);

  // Escape: cancels an active edit first; while a field has focus it is
  // left to the field; otherwise it closes the drawer. Radix only delivers
  // this when the drawer is the topmost layer, so the Copy-to dialog's own
  // Escape never reaches here.
  const onEscapeKeyDown = (e: KeyboardEvent) => {
    if (editing) {
      e.preventDefault();
      setEditing(false);
      setDraft("");
      return;
    }
    if (renaming) {
      e.preventDefault();
      setRenaming(false);
      return;
    }
    const t = document.activeElement;
    if (t instanceof HTMLElement && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.isContentEditable))
      e.preventDefault();
  };

  // Outside pointer-down: a click on a toast (dismissing an error raised
  // from here) is not a dismissal; the expanded backdrop collapses back to
  // the drawer; an active edit or the Copy-to dialog keep the drawer open;
  // otherwise it closes.
  const onPointerDownOutside = (e: Event) => {
    const target = e.target;
    if (target instanceof Element && target.closest("[data-toast-viewport]")) {
      e.preventDefault();
      return;
    }
    if (expanded) {
      e.preventDefault();
      setExpanded(false);
      return;
    }
    if (editing || renaming || copyOpen) e.preventDefault();
  };

  // e/d/p drive edit/delete/pin, ⌘/Ctrl+Enter saves an edit. Bound to the
  // dialog content, so they fire only while the drawer holds focus. Radix
  // portals bubble React events through the tree, so the Copy-to dialog's
  // keystrokes are skipped explicitly.
  const onContentKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (copyOpen) return;
    const t = e.target;
    const typing =
      t instanceof HTMLElement && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.isContentEditable);
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && editing) {
      e.preventDefault();
      onSaveEdit();
      return;
    }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "e" && !editing) {
      e.preventDefault();
      onStartEdit();
    } else if (e.key === "d") {
      e.preventDefault();
      onDelete();
    } else if (e.key === "p") {
      e.preventDefault();
      onPin();
    }
  };

  useEffect(() => {
    setLoading(true);
    setRenaming(false);
    setRestoring(false);
    setDetail(null);
    setHistory(null);
    setRelated(null);
    setLineage(null);
    setProvenance(null);
    inspection.current += 1;
    requested.current = new Set();
    setDone(new Set());
    // A read for a key the drawer has moved off must not land: its value under the new
    // key would be what Edit, Pin and Verify then write back.
    let live = true;
    getMemory(identity, memoryKey, { include_recall_breakdown: true })
      .then((res) => {
        if (!live) return;
        setDetail(res);
        setLoading(false);
      })
      .catch((e) => {
        if (!live) return;
        toast(`Load failed: ${humanizeError(e)}`, "error");
        setLoading(false);
      });
    // Nothing else is read on open. The four secondary sections read when opened (below);
    // the tag vocabulary when the add-tag input is focused. Reading every section on open
    // costs seven reads and about 118 MB of ClickHouse for six kilobytes on the screen.
    return () => {
      live = false;
    };
  }, [identity, memoryKey]);

  useEffect(() => {
    const k = memoryKey;
    const mine = inspection.current;
    const fresh = () => inspection.current === mine;
    const finish = (id: SectionId) => {
      if (fresh()) setDone((d) => new Set(d).add(id));
    };
    const want = (id: SectionId, run: () => Promise<void>) => {
      if (!sections[id] || requested.current.has(id)) return;
      requested.current.add(id);
      run()
        .catch(() => {
          /* handled per section */
        })
        .finally(() => finish(id));
    };
    want("history", () =>
      memoryHistory(identity, k, 8)
        .then((r) => {
          if (fresh()) setHistory(r.rows || []);
        })
        .catch(() => {
          if (fresh()) setHistory([]);
        }),
    );
    want("related", () =>
      memoryRelated(identity, k, 5)
        .then((r) => {
          if (fresh()) setRelated(r.rows || []);
        })
        .catch(() => {
          if (fresh()) setRelated([]);
        }),
    );
    want("lineage", () =>
      memoryLineage(identity, k)
        .then((r) => {
          if (fresh()) setLineage(r.steps || []);
        })
        .catch(() => {
          if (fresh()) setLineage([]);
        }),
    );
    want("provenance", () =>
      getProvenance(identity, k)
        .then((r) => {
          if (fresh()) setProvenance(r);
        })
        .catch(() => {
          if (fresh()) setProvenance(null);
        }),
    );
  }, [identity, memoryKey, sections]);

  // The conflict count is its own effect so a role that resolves after the
  // drawer opened does not re-read the memory. Only when the role here is
  // known to allow conflicts.list: an unknown role sends nothing and the badge is simply
  // absent.
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const conflictsReadable = canCall("conflicts.list", role, whoami);
  // The rename action is offered only to a role that reaches memory.move (WRITER); a
  // viewer never sees a button the server would refuse.
  const canMove = canCall("memory.move", role, whoami);
  const canTogglePin = canCall(detail?.pinned ? "memory.unpin" : "memory.pin", role, whoami);
  const canRestore = canCall("memory.restore", role, whoami);
  useEffect(() => {
    setConflictCount(0);
    if (!conflictsReadable) return;
    let cancelled = false;
    // The keys, not 200 rows with their values.
    conflictKeys({ org: identity.org, namespace: identity.namespace })
      .then((keys) => {
        if (cancelled) return;
        setConflictCount(keys.filter((k) => k === memoryKey).length);
      })
      .catch(() => {
        /* leave at 0 */
      });
    return () => {
      cancelled = true;
    };
  }, [identity, memoryKey, conflictsReadable]);

  const onPin = async () => {
    if (!detail || !canTogglePin) return;
    const wasPinned = detail.pinned;
    setDetail({ ...detail, pinned: !wasPinned });
    try {
      if (wasPinned) {
        const result = await unpinMemory(identity, memoryKey);
        if (!result.unpinned && result.reason !== "not_pinned") {
          throw new Error(result.reason || "memory was not unpinned");
        }
      } else {
        await pinMemory(identity, memoryKey, detail.value, {
          memoryType: detail.memory_type || "semantic",
          // Preserve char_limit — write-through would drop the cap otherwise.
          charLimit: detail.char_limit ?? undefined,
        });
      }
      toast(wasPinned ? "Unpinned" : "Pinned", "ok");
      onChanged();
    } catch (e) {
      setDetail((d) => (d ? { ...d, pinned: wasPinned } : d));
      toast(`Pin failed: ${humanizeError(e)}`, "error");
    }
  };

  // Attest (or revoke). The server picks the actor from the token and binds
  // the attestation to the current content hash; an edit afterwards flips
  // the state to "stale" on its own, so there is nothing to un-set here.
  const onVerify = async () => {
    if (!detail) return;
    const live = detail.verification === "human" || detail.verification === "process";
    try {
      const r = await verifyMemory(identity, memoryKey, {
        revoke: live,
        userId: detail.user_id || undefined,
        sessionId: detail.session_id || undefined,
      });
      setDetail({ ...detail, verified_by: r.verified_by, verified_at: r.verified_at, verification: r.verification });
      toast(live ? "Verification removed" : `Verified as ${r.verified_by}`, "ok");
      onChanged();
    } catch (e) {
      toast(`Verify failed: ${humanizeError(e)}`, "error");
    }
  };

  // Copy the whole memory — key, value, and every metadata field + tags —
  // as a readable text block, matching what the panel shows.
  const onCopyAll = () => {
    if (!detail) return;
    const v = detail.value;
    const valueText = typeof v === "string" ? v : v == null ? "" : JSON.stringify(v, null, 2);
    const lastRecalled = recalledLabel(detail.recall_count, detail.last_recalled_at);
    const tags = detail.tags && detail.tags.length ? detail.tags.join(", ") : "—";
    const block = [
      `Key:           ${memoryKey}`,
      `Type:          ${detail.memory_type || "—"}`,
      `Agent:         ${detail.agent || "No agent (admin / me)"}`,
      `Status:        ${statusLabel(detail.status)}`,
      `Created:       ${detail.created_at ? relativeTime(detail.created_at) : "—"}`,
      `Expires:       ${detail.expires_at ? relativeTime(detail.expires_at) : "Never expires"}`,
      `Last recalled: ${lastRecalled}`,
      `Tags:          ${tags}`,
      ``,
      `Value:`,
      valueText,
    ].join("\n");
    navigator.clipboard.writeText(block);
    toast("Copied memory", "ok");
  };

  const onStartEdit = () => {
    if (!detail) return;
    setDraft(formatValue(detail.value));
    setEditing(true);
  };

  // Persist a tag list outside Edit mode — each click is its own commit.
  const persistTags = async (nextTags: string[]) => {
    if (!detail) return;
    const prev = detail.tags || [];
    setDetail({ ...detail, tags: nextTags });
    try {
      await setMemory(identity, memoryKey, detail.value, {
        memoryType: detail.memory_type || "semantic",
        tags: nextTags,
        charLimit: detail.char_limit ?? undefined,
      });
      onChanged();
    } catch (e) {
      setDetail((d) => (d ? { ...d, tags: prev } : d));
      toast(`Tag update failed: ${humanizeError(e)}`, "error");
    }
  };

  const onRemoveTag = (t: string) => {
    if (!detail) return;
    const next = (detail.tags || []).filter((x) => x !== t);
    void persistTags(next);
  };

  const onAddTag = (raw: string) => {
    if (!detail) return;
    const t = raw.trim().toLowerCase();
    if (!t) return;
    if ((detail.tags || []).includes(t)) return;
    void persistTags([...(detail.tags || []), t]);
  };

  const onChangeExpiry = async (ttlSeconds: number | null) => {
    if (!detail) return;
    try {
      await setMemory(identity, memoryKey, detail.value, {
        memoryType: detail.memory_type || "semantic",
        tags: detail.tags,
        // ttlSeconds=null is "Never expire" — clearExpiry overrides any
        // inherited per-tag retention default.
        ttlSeconds: ttlSeconds ?? undefined,
        clearExpiry: ttlSeconds === null,
        charLimit: detail.char_limit ?? undefined,
      });
      toast(ttlSeconds === null ? "Expiry cleared — never expires" : "Expiry updated", "ok");
      const fresh = await getMemory(identity, memoryKey);
      setDetail(fresh);
      onChanged();
    } catch (e) {
      toast(`Expiry update failed: ${humanizeError(e)}`, "error");
    }
  };

  const onSaveEdit = async () => {
    if (!detail) return;
    try {
      // Parse JSON if it looks like an object/array; else keep the string.
      let nextValue: unknown = draft;
      const trimmed = draft.trim();
      if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
        try {
          nextValue = JSON.parse(draft);
        } catch {
          /* keep raw string */
        }
      }
      await setMemory(identity, memoryKey, nextValue, {
        memoryType: detail.memory_type || "semantic",
        tags: detail.tags,
        charLimit: detail.char_limit ?? undefined,
      });
      toast("Saved", "ok");
      setEditing(false);
      const fresh = await getMemory(identity, memoryKey);
      setDetail(fresh);
      onChanged();
    } catch (e) {
      toast(`Save failed: ${humanizeError(e)}`, "error");
    }
  };

  // After a move: with a redirect the old key still answers (now carrying
  // `canonical_key`), so the row is re-read and the badge appears; without
  // one the old key is tombstoned and the drawer follows the new key.
  const onRenamed = async (r: MemoryMoveResult) => {
    setRenaming(false);
    toast(
      r.redirected
        ? `Renamed to ${r.to_key} — the old key redirects to it`
        : `Renamed to ${r.to_key} — the old key was deleted`,
      "ok",
    );
    onChanged();
    if (r.redirected) {
      try {
        const fresh = await getMemory(identity, memoryKey, { include_recall_breakdown: true });
        setDetail(fresh);
      } catch (e) {
        toast(`Reload failed: ${humanizeError(e)}`, "error");
      }
    } else {
      inspect(r.to_key);
    }
  };

  // A superseded row (Loom retired it, or a later write replaced it) comes back live.
  const onRestore = async () => {
    if (!detail || restoring) return;
    const mine = inspection.current;
    const stale = () => inspection.current !== mine;
    setRestoring(true);
    try {
      const r = await restoreMemory({ ...identity, agent: detail.agent || identity.agent }, memoryKey, {
        user_id: detail.user_id || undefined,
        session_id: detail.session_id || undefined,
        // Only the retirement on screen, never a delete since. A row retired behind one
        // outside the reader's scope shows no successor, so it is guarded by status.
        if_superseded_by: detail.superseded_by,
        only_if_superseded: true,
      });
      // Either way the row changed, so the list behind the drawer is stale too,
      // even if the drawer has moved on to another memory.
      onChanged();
      if (stale()) {
        // Toasts are app-wide, so still report it, naming the key the drawer has left.
        toast(r.restored ? `Restored ${memoryKey}` : `Nothing to restore for ${memoryKey}`, r.restored ? "ok" : "info");
        return;
      }
      // Nothing to restore means the row changed since it was read: show it as it is now.
      toast(r.restored ? "Restored" : "Nothing to restore", r.restored ? "ok" : "info");
      try {
        const fresh = await getMemory(identity, memoryKey, { include_recall_breakdown: true });
        if (!stale()) setDetail(fresh);
      } catch (e) {
        if (!stale()) toast(`Reload failed: ${humanizeError(e)}`, "error");
      }
    } catch (e) {
      if (stale()) toast(`Restore of ${memoryKey} failed: ${humanizeError(e)}`, "error");
      else toast(`Restore failed: ${humanizeError(e)}`, "error");
    } finally {
      if (!stale()) setRestoring(false);
    }
  };

  const onDelete = async () => {
    if (
      !(await confirmAction({
        title: `Delete ${memoryKey}?`,
        message: "The row gets tombstoned but stays restorable for 14 days from Recently deleted.",
        confirmLabel: "Delete",
        danger: true,
      }))
    )
      return;
    try {
      await deleteMemory(identity, memoryKey);
      toast("Deleted", "ok");
      onChanged();
      onClose();
    } catch (e) {
      toast(`Delete failed: ${humanizeError(e)}`, "error");
    }
  };

  return (
    <Dialog.Root
      open
      onOpenChange={(v) => {
        if (!v) onClose();
      }}
    >
      <Dialog.Portal>
        {/* Transparent while docked (the page stays visible, as before); a
            dimmed, blurred backdrop when expanded — pointer-down on it
            collapses back to the drawer. */}
        <Dialog.Overlay
          className={`fixed inset-0 z-20 animate-fade-in motion-reduce:animate-none! ${expanded ? "bg-overlay backdrop-blur-sm" : "bg-transparent"}`}
        />
        <Dialog.Content
          ref={contentRef}
          tabIndex={-1}
          aria-modal="true"
          aria-describedby={undefined}
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            contentRef.current?.focus();
          }}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            opener?.focus();
          }}
          onEscapeKeyDown={onEscapeKeyDown}
          onPointerDownOutside={onPointerDownOutside}
          onKeyDown={onContentKeyDown}
          className={`fixed top-0 bottom-0 right-0 z-30 bg-[var(--color-surface)] border-l border-[var(--color-border)] shadow-2xl animate-slide-in motion-reduce:animate-none! flex flex-col outline-none ${
            expanded ? "w-[90vw] md:w-[85vw] max-w-none" : "w-full md:w-[640px] md:max-w-[90vw]"
          }`}
        >
          <header className="flex items-center justify-between px-6 py-5 border-b border-[var(--color-border)] shrink-0 gap-3">
            <div className="flex items-center gap-2.5 min-w-0 flex-wrap">
              {detail?.pinned && <Star size={16} className="text-warn fill-warn shrink-0" aria-hidden="true" />}
              <Dialog.Title asChild>
                <h2 className="font-mono text-lg text-[var(--color-text)] truncate m-0 font-semibold tracking-tight">
                  {memoryKey}
                </h2>
              </Dialog.Title>
              <LifecycleBadge status={detail?.status} pinned={!!detail?.pinned} />
              {detail?.status === "duplicate" && detail.canonical_key && (
                <RedirectBadge canonicalKey={detail.canonical_key} onFollow={inspect} />
              )}
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <button
                onClick={() => setExpanded((v) => !v)}
                className="p-1.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded-md transition-colors"
                aria-label={expanded ? "Collapse inspector" : "Expand inspector"}
                title={expanded ? "Collapse" : "Expand to full width"}
              >
                {expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
              </button>
              <button
                onClick={onClose}
                className="p-1.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded-md transition-colors"
                aria-label="Close inspector"
              >
                <X size={16} />
              </button>
            </div>
          </header>

          {/* Body */}
          <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6 pb-[80px]">
            {loading && <SkeletonText lines={5} />}

            {!loading && detail && !detail.found && (
              <div className="text-base text-[var(--color-text-muted)]">
                Memory not found. It may have been deleted.
              </div>
            )}

            {!loading && detail && detail.found && (
              <>
                <TrustPanel detail={detail} conflictCount={conflictCount} />
                <CodeLinks links={detail.code ?? []} />
                {renaming && (
                  <RenameForm
                    identity={identity}
                    fromKey={memoryKey}
                    onCancel={() => setRenaming(false)}
                    onRenamed={onRenamed}
                  />
                )}
                <section>
                  {editing ? (
                    <div className="space-y-2">
                      <textarea
                        aria-label="Memory value"
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        rows={Math.min(12, Math.max(3, draft.split("\n").length))}
                        className="w-full text-base font-mono p-3 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-accent)] outline-none resize-y leading-relaxed"
                        autoFocus
                      />
                      <div className="flex items-center gap-2">
                        <button
                          onClick={onSaveEdit}
                          className="inline-flex items-center gap-1 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)]"
                        >
                          <Save size={12} /> Save new version
                        </button>
                        <button
                          onClick={() => setEditing(false)}
                          className="px-3 py-1.5 text-sm font-medium text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] rounded-md"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : (
                    <EncodedAwareValue raw={formatValue(detail.value)} />
                  )}
                </section>

                <section>
                  <h4 className="text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-3">
                    Metadata
                  </h4>
                  <dl className="grid grid-cols-[110px_1fr] gap-y-2 text-sm">
                    <Row label="Type" value={detail.memory_type || "—"} mono />
                    {detail.char_limit != null && (
                      <Row label="Char limit" value={`${detail.char_limit.toLocaleString()} chars`} mono />
                    )}
                    {detail.agent && (
                      <>
                        <dt className="text-[var(--color-text-muted)]">Agent</dt>
                        <dd>
                          <AgentBadge agent={detail.agent} />
                        </dd>
                      </>
                    )}
                    {detail.status === "superseded" && canRestore ? (
                      <>
                        <dt className="text-[var(--color-text-muted)]">Status</dt>
                        <dd className="text-[var(--color-text)] flex items-center gap-2 flex-wrap">
                          {statusLabel(detail.status)}
                          <button
                            onClick={onRestore}
                            disabled={restoring}
                            className="px-2 py-0.5 text-xs font-medium border border-[var(--color-border-strong)] rounded-md hover:bg-[var(--color-surface-2)] disabled:opacity-50"
                          >
                            Restore
                          </button>
                        </dd>
                      </>
                    ) : (
                      <Row label="Status" value={statusLabel(detail.status)} />
                    )}
                    <Row label="Created" value={detail.created_at ? relativeTime(detail.created_at) : "—"} />
                    {detail.updated_at && detail.updated_at !== detail.created_at && (
                      <Row label="Updated" value={relativeTime(detail.updated_at)} />
                    )}
                    <dt className="text-[var(--color-text-muted)]">Expires</dt>
                    <dd>
                      <ExpiryEditor expiresAt={detail.expires_at ?? null} onChange={onChangeExpiry} />
                    </dd>
                    <Row label="Last recalled" value={recalledLabel(detail.recall_count, detail.last_recalled_at)} />
                    {(() => {
                      // Drop sentinel agents (* wildcard, - no-agent default);
                      // they're query artefacts, not real tools.
                      const rows = (detail.recall_by_agent || []).filter(
                        (r) => r.agent && !SENTINEL_AGENTS.has(r.agent),
                      );
                      if (rows.length === 0) return null;
                      return (
                        <>
                          <dt className="text-[var(--color-text-muted)]">Recalled by</dt>
                          <dd>
                            <div className="flex flex-wrap gap-1">
                              {rows.map((r) => (
                                <span
                                  key={r.agent}
                                  className="inline-flex items-center gap-1 text-2xs px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] text-[var(--color-text)]"
                                  title={r.last_at ? `Last by ${r.agent}: ${relativeTime(r.last_at)}` : r.agent}
                                >
                                  <span className="font-mono">{r.agent}</span>
                                  <span className="text-[var(--color-text-muted)]">{r.count}×</span>
                                </span>
                              ))}
                            </div>
                          </dd>
                        </>
                      );
                    })()}
                  </dl>
                </section>

                <section>
                  <h4 className="text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-2">
                    Tags
                  </h4>
                  <div className="flex flex-wrap gap-1.5 items-center">
                    {(detail.tags || []).map((t) => (
                      <span
                        key={t}
                        className="inline-flex items-center gap-1 text-xs pl-2 pr-1 py-0.5 rounded-md bg-[var(--color-surface-2)] text-[var(--color-text)]"
                      >
                        <button
                          onClick={() => searchByTag(t)}
                          className="hover:text-[var(--color-accent)] transition-colors cursor-pointer"
                          title={`Show all memories tagged ${t}`}
                        >
                          <span className="opacity-50 mr-0.5">#</span>
                          {t}
                        </button>
                        <button
                          onClick={() => onRemoveTag(t)}
                          className="p-0.5 text-[var(--color-text-subtle)] hover:text-danger hover:bg-[var(--color-surface-3)] rounded transition-colors"
                          aria-label={`Remove tag ${t}`}
                          title={`Remove tag ${t}`}
                        >
                          <X size={10} />
                        </button>
                      </span>
                    ))}
                    <input
                      aria-label="Add a tag"
                      type="text"
                      placeholder={(detail.tags || []).length > 0 ? "+ add tag" : "Add tags…"}
                      list="loom-inspector-tags"
                      onFocus={() => {
                        // The vocabulary is read when someone goes to add a tag, not on
                        // every open.
                        if (namespaceTags.length === 0)
                          sharedTagVocabulary(identity)
                            .then(setNamespaceTags)
                            .catch(() => setNamespaceTags([]));
                      }}
                      className="text-xs px-2 py-0.5 rounded-md bg-transparent border border-dashed border-[var(--color-border)] outline-none focus:border-[var(--color-accent)] placeholder:text-[var(--color-text-subtle)] w-[110px]"
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === ",") {
                          e.preventDefault();
                          const target = e.currentTarget;
                          onAddTag(target.value);
                          target.value = "";
                        }
                      }}
                      onBlur={(e) => {
                        if (e.target.value.trim()) {
                          onAddTag(e.target.value);
                          e.target.value = "";
                        }
                      }}
                    />
                    {namespaceTags.length > 0 && (
                      <datalist id="loom-inspector-tags">
                        {namespaceTags
                          .filter((t) => !(detail.tags || []).includes(t))
                          .map((t) => (
                            <option key={t} value={t} />
                          ))}
                      </datalist>
                    )}
                  </div>
                </section>

                {/* The Graph is reached from here — an entity on the
                memory opens the Explorer walked from it — not from the rail. */}
                {(detail.entities || []).length > 0 && (
                  <section>
                    <h4 className="text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-2">
                      Entities
                    </h4>
                    <div className="flex flex-wrap gap-1.5 items-center">
                      {(detail.entities || []).map((en) => (
                        <EntityChip key={en} entity={en} />
                      ))}
                    </div>
                  </section>
                )}

                <Disclosure
                  id="provenance"
                  title="Provenance"
                  icon={<GitBranch size={11} />}
                  open={sections.provenance}
                  onToggle={toggleSection}
                >
                  {!done.has("provenance") ? (
                    <SkeletonText lines={2} />
                  ) : provenance && provenance.found ? (
                    <ProvenanceSection
                      provenance={provenance}
                      onInspect={inspect}
                      onOpenThread={openConversationThread}
                      embedded
                    />
                  ) : (
                    <SectionEmpty>Nothing recorded about where this memory came from.</SectionEmpty>
                  )}
                </Disclosure>

                <Disclosure
                  id="related"
                  title="Related"
                  icon={<Link2 size={11} />}
                  open={sections.related}
                  onToggle={toggleSection}
                  count={related ? related.length : undefined}
                >
                  {!done.has("related") || related === null ? (
                    <SkeletonText lines={2} />
                  ) : related.length === 0 ? (
                    <SectionEmpty>No memory shares an entity with this one.</SectionEmpty>
                  ) : (
                    <div className="space-y-1">
                      {related.slice(0, 5).map((r) => (
                        <div
                          key={r.memory_key}
                          className="px-2 py-1.5 rounded-md hover:bg-[var(--color-surface-2)] transition-colors group"
                        >
                          <button
                            onClick={() => inspect(r.memory_key)}
                            className="w-full text-left font-mono text-xs text-[var(--color-text)] truncate group-hover:text-[var(--color-accent)]"
                          >
                            {r.memory_key}
                          </button>
                          {r.shared_entities.length > 0 && (
                            <div className="mt-1 flex flex-wrap gap-1">
                              {r.shared_entities.slice(0, 6).map((en) => (
                                <EntityChip key={en} entity={en} small />
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </Disclosure>

                <Disclosure
                  id="history"
                  title="History"
                  icon={<History size={11} />}
                  open={sections.history}
                  onToggle={toggleSection}
                  count={history && history.length > 1 ? `${history.length} versions` : undefined}
                >
                  {!done.has("history") || history === null ? (
                    <SkeletonText lines={2} />
                  ) : history.length <= 1 ? (
                    <SectionEmpty>Only this version.</SectionEmpty>
                  ) : (
                    <div className="space-y-1">
                      {history.slice(0, 8).map((h, idx) => {
                        const isCurrent = idx === 0;
                        const isOpen = diffVersion === h.version;
                        // The version is int64 epoch ns: past 2^53 as a JS number, so its
                        // digits are not the stored ones. Number the loaded versions instead.
                        const label = `v${history.length - idx}`;
                        const writtenAt = relativeTime(h.written_at || h.created_at);
                        const changed = historyChange(h, previousVersion(history, idx));
                        return (
                          <div key={h.version} className="rounded-md bg-[var(--color-surface-2)] overflow-hidden">
                            <button
                              onClick={() => (isCurrent ? undefined : setDiffVersion(isOpen ? null : h.version))}
                              disabled={isCurrent}
                              className={`w-full px-2 py-1.5 text-left transition-colors ${
                                !isCurrent ? "hover:bg-[var(--color-surface-3)]" : ""
                              }`}
                            >
                              <div className="flex items-center justify-between text-2xs tabular text-[var(--color-text-muted)] mb-0.5 gap-2">
                                <span className="inline-flex items-center gap-1">
                                  {!isCurrent && (isOpen ? <ChevronDown size={9} /> : <ChevronRight size={9} />)}
                                  <span className="font-mono">{label}</span>
                                  {isCurrent && <span className="text-success font-medium ml-1">· current</span>}
                                </span>
                                <span>{writtenAt}</span>
                              </div>
                              <div className="text-xs text-[var(--color-text)] line-clamp-1">
                                {formatValue(h.value).slice(0, 80)}
                                {h.tombstoned_at && <span className="ml-1 text-danger">· tombstoned</span>}
                                {h.redacted_at && <span className="ml-1 text-warn">· redacted</span>}
                              </div>
                              {changed.length > 0 && (
                                <div
                                  data-testid="history-change"
                                  className="text-2xs text-[var(--color-text-muted)] [overflow-wrap:anywhere]"
                                >
                                  {changed.join(" · ")}
                                </div>
                              )}
                            </button>
                            {isOpen && history[0] && (
                              <div className="px-2 pb-2">
                                <DiffView
                                  prev={formatValue(h.value)}
                                  next={formatValue(history[0].value)}
                                  prevLabel={`${label} (${writtenAt})`}
                                  nextLabel={`v${history.length} (current)`}
                                />
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </Disclosure>

                <Disclosure
                  id="lineage"
                  title="Lineage"
                  icon={<GitBranch size={11} />}
                  open={sections.lineage}
                  onToggle={toggleSection}
                >
                  {!done.has("lineage") || lineage === null ? (
                    <SkeletonText lines={2} />
                  ) : lineage.length === 0 ? (
                    <SectionEmpty>No lineage steps.</SectionEmpty>
                  ) : (
                    <div className="space-y-2 pl-2 border-l border-[var(--color-border)]">
                      {lineage.map((s, i) => (
                        <div key={i} className="text-xs -ml-[5px] pl-3 relative">
                          <span className="absolute left-[-3px] top-1.5 w-1.5 h-1.5 rounded-full bg-[var(--color-accent)]" />
                          <div className="flex items-center gap-2">
                            <span className="font-mono font-medium text-[var(--color-text)]">{s.step}</span>
                            <span className="text-[var(--color-text-muted)]">{s.actor}</span>
                            <span className="text-[var(--color-text-subtle)] tabular ml-auto">
                              {relativeTime(s.ts)}
                            </span>
                          </div>
                          {s.detail && <div className="text-2xs text-[var(--color-text-muted)] mt-0.5">{s.detail}</div>}
                        </div>
                      ))}
                    </div>
                  )}
                </Disclosure>
              </>
            )}
          </div>

          {/* Sticky footer actions */}
          {detail && detail.found && (
            <footer className="absolute bottom-0 left-0 right-0 px-5 py-3 border-t border-[var(--color-border)] bg-[var(--color-surface)]/95 backdrop-blur flex flex-wrap items-center gap-1.5 shrink-0">
              {!editing && (
                <button
                  onClick={onStartEdit}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md hover:bg-[var(--color-surface-2)] text-[var(--color-text)] transition-colors"
                >
                  <Pencil size={13} /> Edit
                </button>
              )}
              {canTogglePin && (
                <button
                  onClick={onPin}
                  className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
                    detail.pinned
                      ? "bg-warn/15 text-warn hover:bg-warn/15"
                      : "hover:bg-[var(--color-surface-2)] text-[var(--color-text)]"
                  }`}
                >
                  <Star size={13} className={detail.pinned ? "fill-warn text-warn" : ""} />{" "}
                  {detail.pinned ? "Unpin" : "Pin"}
                </button>
              )}
              <button
                onClick={onVerify}
                title={
                  detail.verification === "stale"
                    ? `Verified by ${detail.verified_by}, then edited — re-verify to confirm the current content`
                    : detail.verification === "human" || detail.verification === "process"
                      ? `Verified by ${detail.verified_by}${detail.verified_at ? " · " + relativeTime(detail.verified_at) : ""} — click to remove`
                      : "Attest that this memory is true, as you"
                }
                className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
                  detail.verification === "human" || detail.verification === "process"
                    ? "bg-success/15 text-success hover:bg-success/15"
                    : detail.verification === "stale"
                      ? "bg-warn/15 text-warn hover:bg-warn/15"
                      : "hover:bg-[var(--color-surface-2)] text-[var(--color-text)]"
                }`}
              >
                {detail.verification === "stale" ? <ShieldAlert size={13} /> : <ShieldCheck size={13} />}
                {detail.verification === "human" || detail.verification === "process"
                  ? "Verified"
                  : detail.verification === "stale"
                    ? "Re-verify"
                    : "Verify"}
              </button>
              <button
                onClick={() => setCopyOpen(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md hover:bg-[var(--color-surface-2)] text-[var(--color-text)] transition-colors"
                title="Copy to another namespace"
              >
                <FolderInput size={13} /> Copy to…
              </button>
              {canMove && !editing && (
                <button
                  type="button"
                  onClick={() => setRenaming((v) => !v)}
                  aria-expanded={renaming}
                  aria-label="Rename (keep redirect)"
                  className={`inline-flex items-center gap-1.5 px-3 py-1.5 min-h-9 text-sm font-medium rounded-md transition-colors ${
                    renaming
                      ? "bg-[var(--color-accent-soft)] text-[var(--color-accent)]"
                      : "hover:bg-[var(--color-surface-2)] text-[var(--color-text)]"
                  }`}
                  title="Move this memory to a new key; the old key keeps answering with a redirect"
                >
                  <ArrowRightLeft size={13} aria-hidden="true" /> Rename
                </button>
              )}
              <button
                onClick={onCopyAll}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md hover:bg-[var(--color-surface-2)] text-[var(--color-text)] transition-colors"
                title="Copy the whole memory — key, value, metadata and tags — to the clipboard"
              >
                <Copy size={13} /> Copy
              </button>
              <div className="flex-1" />
              <button
                onClick={onDelete}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md text-danger hover:bg-danger/15 transition-colors"
              >
                <Trash2 size={13} /> Delete
              </button>
            </footer>
          )}
          <CopyToNamespaceDialog
            open={copyOpen}
            onOpenChange={setCopyOpen}
            source={identity}
            memoryKey={memoryKey}
            onCopied={onChanged}
          />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** Map the backend's status enum to a human label. */
/** A memory's run_id, shown as the "trigger" that caused it to be written.
 *  Loom is app-neutral: it displays the id verbatim and only linkifies it when
 *  the app itself supplied an absolute URL — it makes no assumptions about what
 *  the id means or where it points. */
function triggerFrom(runId: string | null | undefined): { label: string; url?: string } | null {
  const r = (runId || "").trim();
  if (!r || r === "-") return null;
  return /^https?:\/\/\S+$/i.test(r) ? { label: r, url: r } : { label: r };
}

/** Compact display for a long URL — `host/…/last/segments` — link keeps the full URL. */
function shortUrl(u: string): string {
  try {
    const url = new URL(u);
    const seg = url.pathname.split("/").filter(Boolean);
    const tail = seg.slice(-2).join("/");
    return url.hostname + "/" + (seg.length > 2 ? "…/" : "") + tail;
  } catch {
    return u;
  }
}

/** One grouped "Provenance" block: where a memory came from and what led to it —
 *  Source (origin/stage or conversation), Trigger (run_id), Cited source, and
 *  authored derived_from/etc. Links. Renders nothing when there's no signal. */
function ProvenanceSection({
  provenance,
  onInspect,
  onOpenThread,
  embedded = false,
}: {
  provenance: MemoryProvenance | null;
  onInspect: (key: string) => void;
  onOpenThread: (threadId: string) => void;
  /** Inside a Disclosure: the heading is the disclosure's. */
  embedded?: boolean;
}) {
  if (!provenance || !provenance.found) return null;
  const p = provenance;
  const showSource = !!p.source_kind && p.source_kind !== "manual";
  const trig = triggerFrom(p.run_id);
  const cited = p.source_resource_id;
  const links = p.links || [];
  if (!showSource && !trig && !cited && links.length === 0) return null;

  const link = "text-[var(--color-accent)] hover:underline break-all";
  const isUrl = (s: string) => /^https?:\/\/\S+$/i.test(s);

  return (
    <section>
      {!embedded && (
        <h4 className="text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-2 flex items-center gap-1.5">
          <GitBranch size={11} /> Provenance
        </h4>
      )}
      <dl className="grid grid-cols-[92px_1fr] gap-y-1.5 text-xs mb-2">
        {showSource && (
          <>
            <dt className="text-[var(--color-text-muted)]">Source</dt>
            <dd>
              {p.source_kind === "conversation" && p.thread_exists ? (
                <button
                  onClick={() => onOpenThread(p.thread_id)}
                  className={link}
                  title="Open the conversation that produced this memory"
                >
                  {p.thread_title || "View source conversation"} ↗
                </button>
              ) : p.source_kind === "conversation" ? (
                <span title="The source conversation is no longer retained (recording off, or expired past the 180-day window)">
                  From a conversation · <span className="text-[var(--color-text-subtle)]">transcript not retained</span>
                </span>
              ) : (
                <span title={p.source_trace}>{p.source_label}</span>
              )}
            </dd>
          </>
        )}
        {trig && (
          <>
            <dt className="text-[var(--color-text-muted)]">Trigger</dt>
            <dd>
              {trig.url ? (
                <a href={trig.url} target="_blank" rel="noopener noreferrer" className={link} title={trig.url}>
                  {shortUrl(trig.url)} ↗
                </a>
              ) : (
                <span>{trig.label}</span>
              )}
            </dd>
          </>
        )}
        {cited && (
          <>
            <dt className="text-[var(--color-text-muted)]">Cited source</dt>
            <dd>
              {isUrl(cited) ? (
                <a href={cited} target="_blank" rel="noopener noreferrer" className={link} title={cited}>
                  {shortUrl(cited)} ↗
                </a>
              ) : (
                <span className="font-mono break-all">{cited}</span>
              )}
            </dd>
          </>
        )}
      </dl>
      {links.length > 0 && (
        <div className="space-y-1">
          {links.map((lk, i) => (
            <button
              key={`${lk.memory_key}-${i}`}
              onClick={() => onInspect(lk.memory_key)}
              className="w-full text-left px-2 py-1.5 rounded-md hover:bg-[var(--color-surface-2)] transition-colors group"
              title="Open the linked memory"
            >
              <div className="flex items-center gap-1.5 text-2xs text-[var(--color-text-muted)] mb-0.5">
                <span>{lk.direction === "out" ? "→" : "←"}</span>
                <span>{lk.relation || "linked"}</span>
                {lk.label && <span className="text-[var(--color-text-subtle)]">· {lk.label}</span>}
              </div>
              <div className="font-mono text-xs text-[var(--color-text)] truncate group-hover:text-[var(--color-accent)]">
                {lk.memory_key}
              </div>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

/** "N× · <when>" for a recalled memory, else "Never recalled". A zero count
 *  or the epoch sentinel (ClickHouse toDateTime(0) → 1970-01-01) both mean
 *  never — the sentinel is truthy, so a plain `? :` would print "01/01/1970". */
function recalledLabel(recallCount: number | undefined, lastRecalledAt: string | null | undefined): string {
  const n = recallCount ?? 0;
  if (n === 0 || !lastRecalledAt || new Date(lastRecalledAt).getFullYear() <= 1970) return "Never recalled";
  return `${n}× · ${relativeTime(lastRecalledAt)}`;
}

function statusLabel(s?: string): string {
  switch (s) {
    case "":
    case undefined:
    case "active":
      return "Active";
    case "conflicted":
      return "Conflicted — needs review";
    case "tombstoned":
      return "Deleted";
    case "duplicate":
      return "Duplicate of another memory";
    case "superseded":
      return "Superseded by a newer version";
    case "rejected":
      return "Rejected — not promoted";
    default:
      return s;
  }
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <>
      <dt className="text-[var(--color-text-muted)]">{label}</dt>
      <dd className={`text-[var(--color-text)] ${mono ? "font-mono text-xs" : ""}`}>{value}</dd>
    </>
  );
}

/**
 * Warning panel for conflicts and judge-uncertain extractions.
 * Reconfirmation count + last_recalled_at live in the Metadata section,
 * so when there's nothing to warn about the panel doesn't render.
 */
/** The stored value, with a decode toggle when an agent base64-encoded it. Display only —
 *  the stored bytes are never rewritten. */
function EncodedAwareValue({ raw }: { raw: string }) {
  const [showDecoded, setShowDecoded] = useState(true);
  const encoded = useMemo(() => detectEncodedValue(raw), [raw]);
  if (!encoded) {
    return (
      <pre className="text-base font-mono whitespace-pre-wrap break-words text-[var(--color-text)] bg-[var(--color-surface-2)] rounded-lg p-3 leading-relaxed">
        {raw}
      </pre>
    );
  }
  const what =
    encoded.kind === "envelope"
      ? `base64 envelope (field "${encoded.field}"${encoded.encoding ? `, encoding "${encoded.encoding}"` : ""})`
      : "whole value base64-encoded";
  return (
    <div className="space-y-2">
      <div
        className="flex items-start gap-2 text-xs text-[var(--color-text-muted)] bg-[var(--color-warn-soft,var(--color-surface-2))] border border-[var(--color-border)] rounded-lg px-3 py-2"
        role="note"
      >
        <AlertTriangle size={14} className="text-[var(--color-warn)] shrink-0 mt-0.5" aria-hidden />
        <div className="min-w-0 flex-1">
          <span className="font-medium text-[var(--color-text)]">Agent-encoded value</span> — {what}. Loom stores values
          verbatim and hashes them itself; base64 defeats search, fact extraction and dedup. Ask the agent to write
          plain text or JSON.
        </div>
        <button
          type="button"
          onClick={() => setShowDecoded((v) => !v)}
          className="shrink-0 px-2 py-0.5 text-xs font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] text-[var(--color-text)]"
          aria-pressed={showDecoded}
        >
          {showDecoded ? "Show stored" : "Show decoded"}
        </button>
      </div>
      <pre className="text-base font-mono whitespace-pre-wrap break-words text-[var(--color-text)] bg-[var(--color-surface-2)] rounded-lg p-3 leading-relaxed">
        {showDecoded ? encoded.decoded : raw}
      </pre>
    </div>
  );
}

function TrustPanel({ detail, conflictCount }: { detail: MemoryDetail; conflictCount: number }) {
  const sourceTrace = detail.source_trace || "";
  const verdictUncertain = /verdict=uncertain/i.test(sourceTrace);

  if (conflictCount === 0 && !verdictUncertain) return null;

  const primary =
    conflictCount > 0
      ? {
          label:
            conflictCount === 1 ? "Conflicts with 1 other memory" : `Conflicts with ${conflictCount} other memories`,
        }
      : { label: "Judge flagged this as uncertain at extraction" };

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
      <div className="flex items-center gap-2.5 px-4 py-3 text-[var(--color-warn)] bg-[var(--color-surface-2)]">
        <AlertTriangle size={16} className="shrink-0" />
        <span className="text-base font-medium">{primary.label}</span>
      </div>
      {conflictCount > 0 && verdictUncertain && (
        <div className="px-4 py-2 border-t border-[var(--color-border)] text-xs text-[var(--color-text-muted)] flex items-center gap-1.5">
          <AlertTriangle size={11} className="text-[var(--color-warn)]" />
          Judge also flagged this as uncertain at extraction.
        </div>
      )}
    </section>
  );
}

const CODE_STATE: Record<CodeLink["state"], { tone: "accent" | "warn" | "danger" | "muted"; label: string }> = {
  fresh: { tone: "muted", label: "Current" },
  moved: { tone: "accent", label: "Moved" },
  changed: { tone: "warn", label: "Code changed" },
  unresolved: { tone: "warn", label: "Possibly renamed" },
  missing: { tone: "danger", label: "Code removed" },
};

/** The code the memory is linked to, each with whether that code still matches it. */
function CodeLinks({ links }: { links: CodeLink[] }) {
  if (links.length === 0) return null;
  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3">
      <div className="text-xs font-medium text-[var(--color-text-muted)] mb-2">Linked code</div>
      <ul className="space-y-1.5">
        {links.map((l) => {
          const state = CODE_STATE[l.state] ?? CODE_STATE.fresh;
          const since = l.state === "changed" && l.changed_since ? ` since ${l.changed_since.slice(0, 7)}` : "";
          return (
            <li key={`${l.code_namespace}:${l.path}:${l.symbol}`} className="min-w-0">
              <div className="flex items-center gap-2 min-w-0">
                <span
                  className="font-mono text-xs truncate"
                  title={`${l.code_namespace} · ${l.path}${l.symbol ? ` · ${l.symbol}` : ""}`}
                >
                  {l.symbol || l.path}
                </span>
                <Badge tone={state.tone} label={state.label + since} />
              </div>
              {l.suggestion && <p className="mt-1 text-xs text-[var(--color-text-muted)]">Suggested: {l.suggestion}</p>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

const EXPIRY_PRESETS: Array<{ label: string; ttl: number | null }> = [
  { label: "Never", ttl: null },
  { label: "1 hour", ttl: 60 * 60 },
  { label: "1 day", ttl: 60 * 60 * 24 },
  { label: "1 week", ttl: 60 * 60 * 24 * 7 },
  { label: "30 days", ttl: 60 * 60 * 24 * 30 },
  { label: "90 days", ttl: 60 * 60 * 24 * 90 },
  { label: "1 year", ttl: 60 * 60 * 24 * 365 },
];

function ExpiryEditor({
  expiresAt,
  onChange,
}: {
  expiresAt: string | null;
  onChange: (ttlSeconds: number | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const current = expiresAt ? `Expires ${relativeTime(expiresAt)}` : "Never expires";
  const isExpired = expiresAt ? new Date(expiresAt).getTime() < Date.now() : false;

  return (
    <div className="inline-flex items-center gap-2">
      <span className={isExpired ? "text-danger" : "text-[var(--color-text)]"}>{isExpired ? "Expired" : current}</span>
      <div className="relative">
        <button
          onClick={() => setOpen((v) => !v)}
          className="text-2xs px-1.5 py-0.5 rounded hover:bg-[var(--color-surface-2)] text-[var(--color-text-muted)]"
          aria-haspopup="true"
          aria-expanded={open}
        >
          change ▾
        </button>
        {open && (
          <>
            {/* Transparent backdrop — next click closes the popover */}
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} aria-hidden />
            <div className="absolute right-0 top-full mt-1 z-50 min-w-[150px] rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] shadow-xl py-1">
              {EXPIRY_PRESETS.map((opt) => (
                <button
                  key={opt.label}
                  onClick={() => {
                    setOpen(false);
                    onChange(opt.ttl);
                  }}
                  className="w-full text-left px-3 py-1.5 text-sm hover:bg-[var(--color-surface-2)]"
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** "redirects to <key>" on a `status='duplicate'` row whose payload names
 * its canonical: the badge is the way to the row that answers for this one. */
function RedirectBadge({ canonicalKey, onFollow }: { canonicalKey: string; onFollow: (key: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onFollow(canonicalKey)}
      aria-label={`Redirects to ${canonicalKey}; open it`}
      title="This key answers with the row it redirects to. Open the canonical."
      className="inline-flex items-center gap-1 max-w-full min-h-9 px-2 rounded-full border text-2xs font-medium tracking-tight bg-[var(--color-accent-soft)] text-[var(--color-accent)] border-[var(--color-accent)]/30 hover:underline pointer-coarse:min-h-11"
      data-testid="redirect-badge"
    >
      <CornerDownRight size={11} aria-hidden="true" />
      <span className="shrink-0">redirects to </span>
      <span className="font-mono truncate">{canonicalKey}</span>
    </button>
  );
}

/** "Rename (keep redirect)": `to_key`, the redirect checkbox, a dry-run preview and the
 *  real move — all through
 * `memory.move`, never a delete-and-rewrite from here. */
function RenameForm({
  identity,
  fromKey,
  onCancel,
  onRenamed,
}: {
  identity: Identity;
  fromKey: string;
  onCancel: () => void;
  onRenamed: (r: MemoryMoveResult) => void;
}) {
  const [toKey, setToKey] = useState(fromKey);
  const [keepRedirect, setKeepRedirect] = useState(true);
  const [preview, setPreview] = useState<MemoryMoveResult | null>(null);
  const [busy, setBusy] = useState<"preview" | "rename" | null>(null);
  const target = toKey.trim();
  const valid = target.length > 0 && target !== fromKey;

  const call = async (dryRun: boolean) => {
    setBusy(dryRun ? "preview" : "rename");
    try {
      const r = await moveMemory(identity, fromKey, target, { keepRedirect, dryRun });
      if (dryRun) setPreview(r);
      else onRenamed(r);
    } catch (e) {
      toast(`${dryRun ? "Preview" : "Rename"} failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(null);
    }
  };

  const field =
    "w-full min-h-9 px-3 rounded-md bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none font-mono text-sm text-[var(--color-text)]";
  const btn =
    "inline-flex items-center justify-center gap-1.5 min-h-9 px-3 text-sm font-medium rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed pointer-coarse:min-h-11";

  return (
    <section
      aria-labelledby="rename-heading"
      className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-2)]/50 p-4 space-y-3"
      data-testid="rename-form"
    >
      <h4
        id="rename-heading"
        className="text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)] flex items-center gap-1.5"
      >
        <ArrowRightLeft size={11} aria-hidden="true" /> Rename (keep redirect)
      </h4>
      <label className="block text-sm">
        <span className="block text-[var(--color-text-muted)] mb-1">New key</span>
        <input
          aria-label="New key"
          value={toKey}
          onChange={(e) => {
            setToKey(e.target.value);
            setPreview(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && valid && !busy) {
              e.preventDefault();
              void call(true);
            }
          }}
          className={field}
          autoFocus
          spellCheck={false}
        />
      </label>
      <label className="flex items-center gap-2 text-sm text-[var(--color-text)] min-h-9 cursor-pointer">
        <input
          type="checkbox"
          checked={keepRedirect}
          onChange={(e) => {
            setKeepRedirect(e.target.checked);
            setPreview(null);
          }}
          className="h-4 w-4 accent-[var(--color-accent)]"
        />
        Keep old key readable (redirect)
      </label>
      <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">
        {keepRedirect
          ? "A reader of the old key keeps getting the old value, with the new key named; nothing changes under an agent that still reads it."
          : "The old key is deleted (restorable for 14 days). An agent that still reads it will get “not found”."}
      </p>
      {!valid && target === fromKey && (
        <p className="text-xs text-[var(--color-text-subtle)]">Type a different key to rename.</p>
      )}
      {preview && (
        <div
          role="status"
          className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3 text-sm space-y-1"
          data-testid="rename-preview"
        >
          <div className="text-[var(--color-text)]">
            Would write <span className="font-mono">{preview.to_key}</span>
            {preview.memory_type && (
              <>
                {" "}
                as <span className="font-mono">{preview.memory_type}</span>
              </>
            )}
            {preview.redirected || keepRedirect ? (
              <>
                {" "}
                and leave <span className="font-mono">{preview.from_key}</span> redirecting to it.
              </>
            ) : (
              <>
                {" "}
                and delete <span className="font-mono">{preview.from_key}</span>.
              </>
            )}
          </div>
          {preview.value_preview && (
            <pre className="text-xs font-mono whitespace-pre-wrap break-words text-[var(--color-text-muted)] max-h-32 overflow-y-auto">
              {preview.value_preview}
            </pre>
          )}
          {preview.advisories.length > 0 && (
            <ul className="text-xs text-[var(--color-warn)] list-disc pl-4">
              {preview.advisories.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void call(true)}
          disabled={!valid || busy !== null}
          className={`${btn} border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] text-[var(--color-text)]`}
        >
          {busy === "preview" ? "Previewing…" : "Preview"}
        </button>
        <button
          type="button"
          onClick={() => void call(false)}
          disabled={!valid || busy !== null}
          className={`${btn} bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)]`}
        >
          {busy === "rename" ? "Renaming…" : "Rename"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className={`${btn} text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)]`}
        >
          Cancel
        </button>
      </div>
    </section>
  );
}

/** Lifecycle-state badge for the Inspector header. */
function LifecycleBadge({ status, pinned }: { status?: string; pinned: boolean }) {
  // 'active' is the default — only badge it when also pinned.
  if (!status || status === "active") {
    if (pinned) {
      return (
        <Badge
          tone="accent"
          label="Pinned · authoritative"
          title="Pinned memories block conflicting writes from non-pinned candidates. Edit the pinned row explicitly to change the value."
        />
      );
    }
    return null;
  }
  switch (status) {
    case "conflicted":
      return (
        <Badge
          tone="warn"
          label="Conflicted"
          title="This memory disagrees with one or more peers. Resolve via the Review queue."
        />
      );
    case "duplicate":
      return (
        <Badge
          tone="muted"
          label="Duplicate"
          title="Exact-text duplicate of an older row. Kept for audit; the original is canonical."
        />
      );
    case "superseded":
      return (
        <Badge
          tone="muted"
          label="Superseded"
          title="Replaced by a newer version of the same memory_key. History shows the chain."
        />
      );
    case "tombstoned":
      return (
        <Badge
          tone="danger"
          label="Deleted"
          title="Tombstoned. Searchable history is preserved for 14 days before TTL hard-delete."
        />
      );
    case "rejected":
      return (
        <Badge
          tone="danger"
          label="Rejected"
          title="The write-time LLM judge rejected this candidate. It never became active."
        />
      );
    default:
      return <Badge tone="muted" label={status} />;
  }
}

function Badge({
  tone,
  label,
  title,
}: {
  tone: "accent" | "warn" | "danger" | "muted";
  label: string;
  title?: string;
}) {
  const cls =
    tone === "accent"
      ? "bg-[var(--color-accent-soft)] text-[var(--color-accent)] border-[var(--color-accent)]/30"
      : tone === "warn"
        ? "bg-[var(--color-warn)]/15 text-[var(--color-warn)] border-[var(--color-warn)]/30"
        : tone === "danger"
          ? "bg-[var(--color-danger)]/15 text-[var(--color-danger)] border-[var(--color-danger)]/30"
          : "bg-[var(--color-surface-2)] text-[var(--color-text-muted)] border-[var(--color-border)]";
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-2xs font-medium tracking-tight ${cls}`}
    >
      {label}
    </span>
  );
}

/** An entity on the memory, as a link into the Graph walked from it. An anchor, so
 * cmd-click opens the walk in a new tab. */
function EntityChip({ entity, small = false }: { entity: string; small?: boolean }) {
  return (
    <a
      href={graphSeedHref(entity)}
      onClick={onGraphLinkClick(entity)}
      title={`Open the graph around ${entity}`}
      className={`inline-flex items-center gap-1 rounded-md bg-[var(--color-surface-2)] text-[var(--color-text)] hover:text-[var(--color-accent)] transition-colors ${small ? "text-2xs px-1.5 py-px" : "text-xs px-2 py-0.5"}`}
    >
      <Waypoints size={small ? 9 : 10} className="opacity-60" />
      {entity}
    </a>
  );
}
