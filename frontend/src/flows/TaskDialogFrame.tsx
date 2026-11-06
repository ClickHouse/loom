import type { ReactNode } from "react";
import * as RadixDialog from "@radix-ui/react-dialog";
import { Button } from "@clickhouse/click-ui/Button";
import { Label } from "@clickhouse/click-ui/Label";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert } from "../components/ui";

/**
 * The frame the task inbox dialogs share, on `Modal`. Mounted only while open, behind
 * `lazy()`, by their callers.
 */
export function DialogFrame({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  width = 560,
  onCloseAutoFocus,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: string;
  /** One sentence under the title; also the dialog's accessible description. */
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: keyof typeof WIDTH;
  onCloseAutoFocus?: (e: Event) => void;
}) {
  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={description}
      className={`w-full ${WIDTH[width]}`}
      onCloseAutoFocus={onCloseAutoFocus}
    >
      <div className="space-y-4">{children}</div>
      {footer && <ModalFooter>{footer}</ModalFooter>}
    </Modal>
  );
}

// Literal classes, so Tailwind sees each one.
const WIDTH = {
  460: "max-w-[460px]",
  560: "max-w-[560px]",
  600: "max-w-[600px]",
  620: "max-w-[620px]",
  720: "max-w-[720px]",
  760: "max-w-[760px]",
} as const;

export function ErrorNote({ children }: { children: ReactNode }) {
  return <Alert role="alert" state="danger" text={children} />;
}

export function CancelButton({ label = "Cancel", disabled = false }: { label?: string; disabled?: boolean }) {
  return (
    <RadixDialog.Close asChild>
      <Button type="secondary" htmlType="button" disabled={disabled}>
        {label}
      </Button>
    </RadixDialog.Close>
  );
}

/** Click UI has no date-time field (its DatePicker picks a date only), so this one is native. */
export function DateTimeField({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: ReactNode;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <Label htmlFor={id} className="mb-1 block">
        {label}
      </Label>
      <input id={id} type="datetime-local" value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
