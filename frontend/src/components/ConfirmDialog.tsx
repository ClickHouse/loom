import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AlertTriangle, X } from "lucide-react";

/**
 * Replacement for native `window.confirm()`.
 *
 * The browser's confirm is unstyleable, OS-shaped, and visually
 * jarring against the rest of the product. This is the same
 * "if (confirmed) { ... }" call shape, but renders a real modal
 * matching the design system.
 *
 * Focus opens on the Cancel button, never on the confirm: a dialog that opens with Enter
 * wired to "Delete" turns a reflexive keypress into data loss. Confirming takes a click,
 * or Tab then Enter; Escape and the overlay cancel.
 *
 * Usage:
 *   if (await confirmAction({
 *     title: "Delete this document?",
 *     message: "All chunks will be tombstoned. Recoverable for 14 days.",
 *     confirmLabel: "Delete",
 *     danger: true,
 *   })) {
 *     // ...
 *   }
 */
export interface ConfirmOptions {
  title: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Renders the confirm button in danger tone + adds the warning icon
   *  in the header. Use for destructive actions (delete, revoke, etc). */
  danger?: boolean;
}

interface _PendingConfirm extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

let _listener: ((p: _PendingConfirm) => void) | null = null;

export function confirmAction(opts: ConfirmOptions): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    if (!_listener) {
      // Viewport not mounted (e.g. SSR / test). Fall back to native
      // so the caller's flow still works rather than hanging forever.
      const ok =
        typeof window !== "undefined" && window.confirm(opts.title + (opts.message ? `\n\n${opts.message}` : ""));
      resolve(!!ok);
      return;
    }
    _listener({ ...opts, resolve });
  });
}

export function ConfirmViewport() {
  const [pending, setPending] = useState<_PendingConfirm | null>(null);
  // The same entry, read synchronously: a new request settles the one it replaces, and
  // close() clears it before resolving, so every entry resolves exactly once.
  const pendingRef = useRef<_PendingConfirm | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  // Whatever had focus when the confirm was requested — restored on close.
  // A Radix modal Dialog otherwise hands focus to its Trigger, and this
  // dialog has none, so focus would fall to <body>.
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    _listener = (p) => {
      const superseded = pendingRef.current;
      if (superseded) {
        // A newer request replaces an open confirm: the older one answers "cancelled", so
        // its destructive action never runs and its caller's busy state clears. Focus goes
        // back to whatever opened the first dialog, not to a control inside it.
        superseded.resolve(false);
      } else {
        const active = document.activeElement;
        openerRef.current = active instanceof HTMLElement ? active : null;
      }
      pendingRef.current = p;
      setPending(p);
    };
    return () => {
      _listener = null;
    };
  }, []);

  const close = (ok: boolean) => {
    const current = pendingRef.current;
    if (current) {
      pendingRef.current = null;
      setPending(null);
      current.resolve(ok);
    }
  };

  const open = pending !== null;
  const danger = pending?.danger;
  const confirmLabel = pending?.confirmLabel || (danger ? "Delete" : "Confirm");
  const cancelLabel = pending?.cancelLabel || "Cancel";

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(v) => {
        if (!v) close(false);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[60] bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content
          className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-[60] w-[min(440px,92vw)] rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!"
          onEscapeKeyDown={() => close(false)}
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            cancelRef.current?.focus();
          }}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            openerRef.current?.focus();
          }}
        >
          <div className="flex items-start gap-3 px-5 pt-5 pb-2">
            {danger && (
              <div className="shrink-0 w-9 h-9 rounded-full bg-danger/15 flex items-center justify-center">
                <AlertTriangle size={18} className="text-danger" />
              </div>
            )}
            <div className="flex-1 min-w-0">
              <Dialog.Title className="text-md font-semibold tracking-tight">{pending?.title}</Dialog.Title>
              {pending?.message && (
                <Dialog.Description className="mt-1.5 text-sm text-[var(--color-text-muted)] leading-relaxed">
                  {pending.message}
                </Dialog.Description>
              )}
            </div>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)] -mt-1 -mr-1"
              aria-label="Cancel"
              onClick={() => close(false)}
            >
              <X size={14} />
            </Dialog.Close>
          </div>
          <div className="flex items-center justify-end gap-2 px-5 py-4">
            <button
              ref={cancelRef}
              onClick={() => close(false)}
              className="px-3 py-1.5 text-sm font-medium text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] rounded-md"
            >
              {cancelLabel}
            </button>
            <button
              onClick={() => close(true)}
              className={`px-4 py-1.5 text-sm font-medium rounded-md ${
                danger
                  ? "bg-danger text-on-accent hover:bg-danger/85"
                  : "bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)]"
              }`}
            >
              {confirmLabel}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
