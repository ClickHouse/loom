import type { ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AlertCircle, X } from "lucide-react";

/**
 * The chrome the task inbox dialogs share: the same overlay, panel, header and footer as
 * `AddMemoryDialog`, so the four dialogs of one plane do not each restate it. Mounted
 * only while open, behind `lazy()`, by their callers.
 */
export function DialogFrame({
  open,
  onOpenChange,
  title,
  icon,
  description,
  children,
  footer,
  width = 560,
  onCloseAutoFocus,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: ReactNode;
  icon?: ReactNode;
  /** One sentence under the title; also the dialog's accessible description. */
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
  onCloseAutoFocus?: (e: Event) => void;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content
          onCloseAutoFocus={onCloseAutoFocus}
          className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 max-h-[88vh] overflow-y-auto rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!"
          style={{ width: `min(${width}px, 92vw)` }}
        >
          <div className="flex items-start justify-between gap-3 px-6 py-4 border-b border-[var(--color-border)]">
            <div className="min-w-0">
              <Dialog.Title className="text-lg font-semibold tracking-tight flex items-center gap-2">
                {icon} {title}
              </Dialog.Title>
              {description ? (
                <Dialog.Description className="mt-1 text-xs text-[var(--color-text-muted)] leading-relaxed">
                  {description}
                </Dialog.Description>
              ) : (
                <Dialog.Description className="sr-only">
                  {typeof title === "string" ? title : "Dialog"}
                </Dialog.Description>
              )}
            </div>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)] shrink-0"
              aria-label="Close"
            >
              <X size={16} />
            </Dialog.Close>
          </div>
          <div className="p-6 space-y-4">{children}</div>
          {footer && (
            <div className="flex items-center justify-end gap-2 px-6 py-3 border-t border-[var(--color-border)] bg-[var(--color-surface)]">
              {footer}
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export const INPUT_CLS =
  "w-full text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none";
export const TEXTAREA_CLS = `${INPUT_CLS} resize-y leading-relaxed`;
export const PRIMARY_BTN =
  "inline-flex items-center gap-1.5 px-4 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-40 disabled:cursor-not-allowed";
export const SECONDARY_BTN =
  "inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md border border-[var(--color-border)] text-[var(--color-text)] hover:bg-[var(--color-surface-2)] disabled:opacity-40 disabled:cursor-not-allowed";
export const DANGER_BTN =
  "inline-flex items-center gap-1.5 px-4 py-1.5 text-sm font-medium rounded-md bg-[var(--color-danger)] text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed";

export function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5">
        {label} {hint && <span className="text-[var(--color-text-subtle)] font-normal">({hint})</span>}
      </label>
      {children}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-danger)]/30 text-xs text-[var(--color-danger)] flex items-start gap-2"
    >
      <AlertCircle size={13} className="shrink-0 mt-0.5" /> <span>{children}</span>
    </div>
  );
}

export function CancelButton({ label = "Cancel" }: { label?: string }) {
  return (
    <Dialog.Close className="px-3 py-1.5 text-sm font-medium text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] rounded-md">
      {label}
    </Dialog.Close>
  );
}
