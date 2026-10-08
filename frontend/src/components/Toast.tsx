import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, XCircle, Info, X } from "lucide-react";
import { errorRequestId, humanizeError } from "../lib/format";

/**
 * Transient notifications.
 *
 * Each toast owns its timer: success and info leave after 3.5 s, errors
 * after 8 s — an error is read, not glanced at. Hovering or focusing a
 * toast pauses its clock so the message can be read or copied; a close
 * button dismisses it early. The stack is capped at four — a burst of
 * failures drops the oldest rather than covering the page.
 *
 * A toast raised with a `key` replaces the one still showing under the same
 * key (a second expand's result stands alone, it does not stack under the first), and
 * `durationMs` overrides the kind's clock.
 *
 * Announcement: the viewport is a persistent `aria-live="polite"` region,
 * so success and info toasts inserted into it are read; an error toast
 * carries `role="alert"` and is read assertively on insertion. The live
 * attribute on the container also keeps the viewport out of the
 * `aria-hidden` that a Radix modal dialog applies to everything outside
 * itself — a toast raised from inside the inspector stays audible.
 *
 * Layering: dialogs portal to the end of <body> at z-50 (confirm at z-[60]),
 * so a viewport at their z would lose on DOM order and paint under the
 * overlay. It sits at z-[65] — over every dialog, under tooltips (z-[70]).
 * The cards opt back into pointer events, which a modal turns off on <body>,
 * so hover-to-pause still works while a dialog is open.
 */
export type ToastKind = "ok" | "error" | "info";
export interface ToastItem {
  id: number;
  msg: string;
  kind: ToastKind;
  /** Small second line: the request id of a failed call, so a user can quote it to
   *  whoever runs this Loom. */
  detail?: string;
  /** One toast at a time per key: a new toast with the same key replaces
   *  the one showing. */
  key?: string;
  /** Overrides the kind's duration. */
  durationMs?: number;
}

export interface ToastOpts {
  detail?: string;
  key?: string;
  durationMs?: number;
}

let _toastSeq = 1;
const _listeners = new Set<(t: ToastItem) => void>();

/** Trigger a toast from anywhere. */
export function toast(msg: string, kind: ToastKind = "ok", opts: ToastOpts = {}): void {
  const item: ToastItem = {
    id: _toastSeq++,
    msg,
    kind,
    detail: opts.detail,
    key: opts.key,
    durationMs: opts.durationMs,
  };
  _listeners.forEach((l) => l(item));
}

/** An error toast for a thrown value: `context: <humanized error>` with the
 *  request id underneath when the failure carried one. */
export function toastError(context: string, err: unknown): void {
  const rid = errorRequestId(err);
  toast(`${context}: ${humanizeError(err)}`, "error", rid ? { detail: `request id ${rid}` } : {});
}

const MAX_STACK = 4;
const DURATION_MS: Record<ToastKind, number> = { ok: 3500, info: 3500, error: 8000 };

// All toasts use the CH dark palette: tinted surface + brand-matched text.
const KIND_STYLE: Record<ToastKind, { icon: typeof CheckCircle2; cls: string }> = {
  ok: {
    icon: CheckCircle2,
    cls: "text-[var(--color-success)] bg-[var(--color-surface-2)] border-[var(--color-border-strong)]",
  },
  error: {
    icon: XCircle,
    cls: "text-[var(--color-danger)]  bg-[var(--color-surface-2)] border-[var(--color-border-strong)]",
  },
  info: {
    icon: Info,
    cls: "text-[var(--color-accent)]  bg-[var(--color-surface-2)] border-[var(--color-border-strong)]",
  },
};

export function ToastViewport() {
  const [items, setItems] = useState<ToastItem[]>([]);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((x) => x.id !== id));
  }, []);

  useEffect(() => {
    const onToast = (t: ToastItem) => {
      setItems((prev) => [...prev.filter((x) => !t.key || x.key !== t.key), t].slice(-MAX_STACK));
    };
    _listeners.add(onToast);
    return () => {
      _listeners.delete(onToast);
    };
  }, []);

  return (
    <div
      data-toast-viewport=""
      aria-live="polite"
      aria-atomic="false"
      className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[65] flex flex-col gap-2 pointer-events-none"
    >
      {items.map((t) => (
        <ToastCard key={t.id} item={t} onDismiss={dismiss} />
      ))}
    </div>
  );
}

function ToastCard({ item, onDismiss }: { item: ToastItem; onDismiss: (id: number) => void }) {
  const { icon: Icon, cls } = KIND_STYLE[item.kind];
  const remaining = useRef(item.durationMs ?? DURATION_MS[item.kind]);
  const startedAt = useRef(0);
  const timer = useRef<number | null>(null);
  const hovered = useRef(false);
  const focused = useRef(false);

  const pause = useCallback(() => {
    if (timer.current == null) return;
    window.clearTimeout(timer.current);
    timer.current = null;
    remaining.current = Math.max(0, remaining.current - (Date.now() - startedAt.current));
  }, []);

  const resume = useCallback(() => {
    if (timer.current != null || hovered.current || focused.current) return;
    startedAt.current = Date.now();
    timer.current = window.setTimeout(() => onDismiss(item.id), remaining.current);
  }, [item.id, onDismiss]);

  useEffect(() => {
    resume();
    return pause;
  }, [resume, pause]);

  return (
    <div
      role={item.kind === "error" ? "alert" : undefined}
      data-toast-kind={item.kind}
      onMouseEnter={() => {
        hovered.current = true;
        pause();
      }}
      onMouseLeave={() => {
        hovered.current = false;
        resume();
      }}
      onFocus={() => {
        focused.current = true;
        pause();
      }}
      onBlur={(e) => {
        if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return;
        focused.current = false;
        resume();
      }}
      className={`pointer-events-auto animate-fade-in motion-reduce:animate-none! flex items-center gap-2 pl-4 pr-2 py-2 rounded-xl border shadow-lg backdrop-blur ${cls}`}
    >
      <Icon size={16} className="shrink-0" aria-hidden="true" />
      <span className="flex flex-col">
        <span className="text-base font-medium">{item.msg}</span>
        {item.detail && <span className="text-xs font-mono text-[var(--color-text-muted)]">{item.detail}</span>}
      </span>
      <button
        type="button"
        onClick={() => onDismiss(item.id)}
        aria-label="Dismiss notification"
        className="ml-1 p-1.5 rounded-md text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-3)] transition-colors"
      >
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  );
}
