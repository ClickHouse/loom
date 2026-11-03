import { useLayoutEffect, useRef, type ComponentPropsWithoutRef, type FocusEvent, type ReactNode } from "react";
import * as RadixDialog from "@radix-ui/react-dialog";
import { Dialog } from "@clickhouse/click-ui/Dialog";
import { Icon } from "@clickhouse/click-ui/Icon";
import { lastPressed } from "../../lib/clickUiFixes";

/**
 * Modal — the one dialog: Click UI's Dialog (title, description, padding, focus trap,
 * Escape) with a close button, a layered overlay and focus returned to the opener. Click
 * UI's own `showClose` button is named after its icon ("cross"), so Modal draws the same
 * button named "Close"; Click UI's overlay has no z-index, and Radix returns focus only
 * to a `Dialog.Trigger`, which a dialog opened from state has none of. Actions go in a
 * `ModalFooter`: secondary first, the primary last.
 */
type ContentProps = ComponentPropsWithoutRef<typeof Dialog.Content>;

export interface ModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children?: ReactNode;
  /** Width and other layout on the content box, e.g. `max-w-[520px]`. */
  className?: string;
  /** A confirm raised over another dialog sits one layer up. */
  layer?: "dialog" | "confirm";
  /** "dialog" starts on the dialog itself — for a read-only view whose first focusable is
   *  deep in the content. Default: the first field or action. */
  initialFocus?: "first" | "dialog";
  onOpenAutoFocus?: ContentProps["onOpenAutoFocus"];
  onCloseAutoFocus?: ContentProps["onCloseAutoFocus"];
  onEscapeKeyDown?: ContentProps["onEscapeKeyDown"];
  onPointerDownOutside?: ContentProps["onPointerDownOutside"];
}

// Over the inspector drawer (z-30), a dialog takes z-50 and a confirm over it z-[60].
// Toasts are z-[65]; popovers and tooltips z-[70] (styles.css).
const LAYER = { dialog: "z-50", confirm: "z-[60]" } as const;

// Opening focuses the first field or action in the body, as Radix would, or the dialog
// itself when the body has none yet (its fields render after a read, or it holds only
// links, which Radix skips too). Never the close button, which would leave a reader
// starting on "Close" rather than in the content.
const BODY_FOCUSABLE = "input, select, textarea, button:not([data-modal-close]), [tabindex]:not([tabindex='-1'])";

function focusDialog(e: Event) {
  if (!(e.currentTarget instanceof HTMLElement)) return;
  e.preventDefault();
  e.currentTarget.focus();
}

function keepFocusInView(e: FocusEvent<HTMLDivElement>) {
  if (e.target !== e.currentTarget) e.target.scrollIntoView?.({ block: "nearest" });
}

function focusBodyOrDialog(e: Event) {
  const content = e.currentTarget;
  if (!(content instanceof HTMLElement)) return;
  e.preventDefault();
  const first = [...content.querySelectorAll<HTMLElement>(BODY_FOCUSABLE)].find(
    (el) => !el.matches(":disabled") && el.getClientRects().length > 0,
  );
  (first ?? content).focus();
}

export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  className = "",
  layer = "dialog",
  initialFocus = "first",
  onCloseAutoFocus,
  onPointerDownOutside,
  ...handlers
}: ModalProps) {
  // Taken before Radix moves focus into the dialog (its focus effect is a passive one).
  const opener = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (!open) return;
    const active = document.activeElement;
    // On the body (a loading button lost it, or Safari never focused it): the control pressed.
    opener.current = active instanceof HTMLElement && active !== document.body ? active : lastPressed();
  }, [open]);
  // A press on a toast (or the expired session's card among them) is not a dismissal: they
  // sit over every dialog, and a dialog closed by one would lose what was typed into it.
  const pointerDownOutside: ContentProps["onPointerDownOutside"] = (e) => {
    if (e.target instanceof Element && e.target.closest("[data-toast-viewport]")) {
      e.preventDefault();
      return;
    }
    onPointerDownOutside?.(e);
  };
  const returnFocus = (e: Event) => {
    onCloseAutoFocus?.(e);
    if (e.defaultPrevented || !opener.current?.isConnected) return;
    e.preventDefault();
    opener.current.focus();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className={`fixed inset-0 bg-overlay ${LAYER[layer]}`} />
      </RadixDialog.Portal>
      <Dialog.Content
        title={title}
        description={description}
        showOverlay={false}
        className={`${LAYER[layer]} ${className}`}
        onOpenAutoFocus={initialFocus === "dialog" ? focusDialog : focusBodyOrDialog}
        onCloseAutoFocus={returnFocus}
        // Radix's focus trap moves focus without scrolling (its wrap from the last control
        // to the first, too), so a long dialog keeps the focused control in view itself.
        onFocus={keepFocusInView}
        onPointerDownOutside={pointerDownOutside}
        {...handlers}
      >
        {/* First in the DOM, as it is drawn in the header, so Tab follows what is seen.
            Click UI's own close: a large cross with no tooltip, so one Escape closes. */}
        <RadixDialog.Close
          aria-label="Close"
          className="dialog-close absolute top-[var(--click-dialog-space-y)] right-[var(--click-dialog-space-x)]"
          data-modal-close=""
        >
          <Icon name="cross" size="lg" aria-hidden="true" />
        </RadixDialog.Close>
        {children}
      </Dialog.Content>
    </Dialog>
  );
}

export function ModalFooter({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center justify-end gap-2 pt-6">{children}</div>;
}
