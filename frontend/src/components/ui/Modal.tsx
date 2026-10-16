import type { ComponentPropsWithoutRef, ReactNode } from "react";
import * as RadixDialog from "@radix-ui/react-dialog";
import { Dialog } from "@clickhouse/click-ui/Dialog";
import { IconButton } from "../IconButton";

/**
 * Modal — the one dialog: Click UI's Dialog (title, description, padding, focus trap,
 * Escape, focus returned to the opener) with a named close button and a layered overlay.
 * Click UI's own `showClose` renders an icon button with no accessible name, and its
 * overlay has no z-index, so both are Loom's. Actions go in a `ModalFooter`: secondary
 * first, the primary last.
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
  onOpenAutoFocus?: ContentProps["onOpenAutoFocus"];
  onCloseAutoFocus?: ContentProps["onCloseAutoFocus"];
  onEscapeKeyDown?: ContentProps["onEscapeKeyDown"];
  onPointerDownOutside?: ContentProps["onPointerDownOutside"];
}

// Loom's drawers are z-40, so a dialog takes z-50 and a confirm over it z-[60]. Toasts are
// z-[65]; popovers and tooltips z-[70] (styles.css).
const LAYER = { dialog: "z-50", confirm: "z-[60]" } as const;

export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  className = "",
  layer = "dialog",
  ...handlers
}: ModalProps) {
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
        {...handlers}
      >
        <RadixDialog.Close asChild>
          <IconButton icon="cross" label="Close" className="absolute top-5 right-5" />
        </RadixDialog.Close>
        {children}
      </Dialog.Content>
    </Dialog>
  );
}

export function ModalFooter({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center justify-end gap-2 pt-6">{children}</div>;
}
