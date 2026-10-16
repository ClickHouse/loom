import { useEffect, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Modal, ModalFooter } from "./ui/Modal";

/**
 * Replacement for native `window.confirm()`.
 *
 * The browser's confirm is unstyleable, OS-shaped, and visually
 * jarring against the rest of the product. This is the same
 * "if (confirmed) { ... }" call shape, but renders a real modal
 * matching the design system.
 *
 * Click UI's Modal and Buttons. Not Click UI's ConfirmationDialog: it focuses the confirm.
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
  /** Renders the confirm button as Click UI's danger button. Use for destructive actions
   *  (delete, revoke, etc). */
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
    <Modal
      open={open}
      onOpenChange={(v) => {
        if (!v) close(false);
      }}
      title={pending?.title ?? ""}
      description={pending?.message}
      className="max-w-[440px]"
      layer="confirm"
      onOpenAutoFocus={(e) => {
        e.preventDefault();
        cancelRef.current?.focus();
      }}
      onCloseAutoFocus={(e) => {
        e.preventDefault();
        openerRef.current?.focus();
      }}
    >
      <ModalFooter>
        <Button ref={cancelRef} type="secondary" htmlType="button" onClick={() => close(false)}>
          {cancelLabel}
        </Button>
        <Button type={danger ? "danger" : "primary"} htmlType="button" onClick={() => close(true)}>
          {confirmLabel}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
