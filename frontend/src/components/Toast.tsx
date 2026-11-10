import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { IconButton } from "./IconButton";
import { isPublicationPending, PUBLICATION_PENDING, writeWarning } from "../lib/errors";
import { errorRequestId } from "../lib/format";

/**
 * Transient notifications.
 *
 * Each toast owns its timer: success and info leave after 3.5 s, an error after
 * 8 s plus 50 ms a character, at most 20 s (`errorToastMs`) — an error is read,
 * not glanced at. So is a committed write's info line, which gets the error's time.
 * Hovering or focusing a toast pauses its clock so the message can be read or copied; a close button dismisses it early. The stack is capped
 * at four — a burst of failures drops the oldest rather than covering the page.
 *
 * A toast raised with a `key` replaces the one still showing under the same
 * key (a second expand's result stands alone, it does not stack under the first), and
 * `durationMs` overrides the kind's clock.
 *
 * A toast with an `action` (a delete's Undo) has no clock, so a keyboard or screen-reader
 * user has time to reach it (WCAG 2.2.1): it stays until the action runs, it is dismissed,
 * or four later toasts push it off the stack.
 *
 * Announcement: the viewport is a persistent `aria-live="polite"` region,
 * so success and info toasts inserted into it are read; an error toast
 * carries `role="alert"` and is read assertively on insertion. The live
 * attribute on the container also keeps the viewport out of the
 * `aria-hidden` that a Radix modal dialog applies to everything outside
 * itself — a toast raised from inside the inspector stays audible.
 *
 * Click UI's anatomy and place (card, stroke, radius, shadow, 332 px, title and
 * description type, state icons, bottom right) on Loom's own element. Click UI 0.13's
 * Toast has no keyed replacement, no request-id line, and a close button named after its
 * icon ("cross"); its viewport also sits inside a modal's aria-hidden.
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
  /** One button that runs `run` and dismisses the toast. */
  action?: ToastAction;
}

export interface ToastAction {
  label: string;
  run: () => void;
}

export interface ToastOpts {
  detail?: string;
  key?: string;
  durationMs?: number;
  action?: ToastAction;
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
    action: opts.action,
  };
  _listeners.forEach((l) => l(item));
}

/** An error toast for a thrown value: `context: <the server's words>` (the context is the
 *  lead-in, so humanizeError's own is left out) with the request id underneath when the
 *  failure carried one. It stays up long enough to read: 8 s plus 50 ms a character, at
 *  most 20 s, and hovering or focusing it holds it. */
export function toastError(context: string, err: unknown, opts: { read?: boolean } = {}): void {
  // Said before the wording loads, so a chunk that cannot load never turns it into "failed".
  if (isPublicationPending(err)) {
    toastPublicationPending(err);
    return;
  }
  // The wording is a lazy chunk (the boot chunk's budget), loaded by the viewport soon
  // after paint; this import then resolves at once.
  import("../lib/errorWords")
    .then(({ humanizeError }) =>
      toastErrorLine(`${context}: ${humanizeError(err, { bare: true, read: opts.read })}`, err),
    )
    // The wording could not load (a deploy replaced the chunk, the network dropped): the
    // context alone still says what failed.
    .catch(() => {
      const warning = opts.read ? "" : writeWarning(err);
      toastWithId(warning ? `${context}: ${warning}` : `${context}.`, "error", err);
    });
}

/** A write that committed while only lists and search lag: not a failure, so no
 *  "<context>:" in front of it. */
function toastPublicationPending(err: unknown): void {
  toastWithId(PUBLICATION_PENDING, "info", err);
}

/** An error toast whose caller built its own sentence (how much of a batch went through):
 *  the request id underneath and the reading time, as toastError gives every error toast. */
export function toastErrorLine(msg: string, err: unknown): void {
  toastWithId(msg, "error", err);
}

function toastWithId(msg: string, kind: ToastKind, err: unknown): void {
  const rid = errorRequestId(err);
  // An id the line already says (an internal error's) is not printed a second time.
  const detail = rid && !msg.includes(rid) ? { detail: `request id ${rid}` } : {};
  toast(msg, kind, { ...detail, durationMs: errorToastMs(msg) });
}

export function errorToastMs(msg: string): number {
  return Math.min(DURATION_MS.error + 50 * msg.length, 20_000);
}

const MAX_STACK = 4;
const WORDING_WARM_MS = 1500;
const DURATION_MS: Record<ToastKind, number> = { ok: 3500, info: 3500, error: 8000 };

const KIND_ICON: Record<ToastKind, { name: IconName; color: string }> = {
  ok: { name: "check", color: "var(--click-toast-color-icon-success)" },
  error: { name: "warning", color: "var(--click-toast-color-icon-danger)" },
  info: { name: "info-in-circle", color: "var(--click-toast-color-icon-default)" },
};

/** A dialog's outside-press handler: true (and the press kept from dismissing it) when the
 *  press landed on the toasts, which sit over every dialog; a dialog closed by a press on a
 *  toast, or on the expired session's card, would lose what was typed into it. */
export function keepOpenOnToasts(e: Event): boolean {
  if (!(e.target instanceof Element) || !e.target.closest("[data-toast-viewport]")) return false;
  e.preventDefault();
  return true;
}

/** `pinned`: a card that stays above the toasts until its owner removes it (the expired
 *  session's), at their layer and inside their live region, so a modal neither covers nor
 *  silences it. */
export function ToastViewport({ pinned = null }: { pinned?: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  // Load the error wording soon after the shell paints, so an error toast never fetches it:
  // in a tab opened before a redeploy that fetch would miss, and the toast would fall back
  // to its context alone.
  useEffect(() => {
    const t = window.setTimeout(() => {
      import("../lib/errorWords").catch(() => {});
    }, WORDING_WARM_MS);
    return () => window.clearTimeout(t);
  }, []);

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
      className="fixed bottom-4 right-4 z-[65] flex flex-col items-end gap-2 pointer-events-none"
    >
      {pinned}
      {items.map((t) => (
        <ToastCard key={t.id} item={t} onDismiss={dismiss} />
      ))}
    </div>
  );
}

function ToastCard({ item, onDismiss }: { item: ToastItem; onDismiss: (id: number) => void }) {
  const icon = KIND_ICON[item.kind];
  const action = item.action;
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
    if (action || timer.current != null || hovered.current || focused.current) return;
    startedAt.current = Date.now();
    timer.current = window.setTimeout(() => onDismiss(item.id), remaining.current);
  }, [item.id, action, onDismiss]);

  useEffect(() => {
    resume();
    return pause;
  }, [resume, pause]);

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: hover and focus pause the dismiss timer (WCAG 2.2.1); the toast's own controls are buttons
    // biome-ignore lint/a11y/noNoninteractiveElementInteractions: as above, an alert toast pauses while it is hovered or focused
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
      className="toast pointer-events-auto animate-fade-in flex items-start"
    >
      <Icon name={icon.name} size="sm" color={icon.color} aria-hidden="true" className="shrink-0 mt-0.5" />
      <span className="flex flex-col flex-1 min-w-0">
        <span className="toast-title break-words">{item.msg}</span>
        {item.detail && <span className="toast-detail text-xs font-mono">{item.detail}</span>}
      </span>
      {action && (
        <Button
          type="secondary"
          htmlType="button"
          onClick={() => {
            onDismiss(item.id);
            action.run();
          }}
        >
          {action.label}
        </Button>
      )}
      <IconButton icon="cross" size="sm" label="Dismiss notification" onClick={() => onDismiss(item.id)} />
    </div>
  );
}
