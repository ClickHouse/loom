import * as Dialog from "@radix-ui/react-dialog";
import { Keyboard, X } from "lucide-react";

interface Shortcut {
  key: string;
  label: string;
}

const GLOBAL: Shortcut[] = [
  { key: "⌘K", label: "Focus the universal search bar" },
  { key: "?", label: "Show keyboard shortcuts" },
  { key: "/", label: "Focus search (on Memories)" },
  { key: "Esc", label: "Close inspector / dialog" },
];

const GOTO: Shortcut[] = [
  { key: "g m", label: "Go to Memories" },
  { key: "g l", label: "Go to Library (or g k)" },
  { key: "g d", label: "Go to Spaces (browse and join)" },
  { key: "g r", label: "Go to Review queue" },
  { key: "g a", label: "Go to Activity" },
  { key: "g s", label: "Go to Setup" },
];

const LIST: Shortcut[] = [
  { key: "j / ↓", label: "Next item" },
  { key: "k / ↑", label: "Previous item" },
  { key: "Enter", label: "Open item" },
  { key: "Space", label: "Toggle select (Memories)" },
];

const INSPECTOR: Shortcut[] = [
  { key: "e", label: "Edit memory value" },
  { key: "⌘↵", label: "Save edit" },
  { key: "Esc", label: "Cancel edit" },
  { key: "d", label: "Delete memory (confirm)" },
  { key: "p", label: "Toggle pin" },
];

const SEARCH: Shortcut[] = [
  { key: "tag:work", label: "Memories tagged work (partial OK)" },
  { key: "namespace:qa", label: "Jump to a namespace" },
  { key: "↑ / ↓", label: "Navigate results" },
  { key: "Enter", label: "Open result" },
];

export function HelpDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(520px,90vw)] max-h-[80vh] overflow-y-auto rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
          <div className="flex items-center justify-between px-5 py-3 border-b border-[var(--color-border)]">
            <Dialog.Title className="text-md font-semibold tracking-tight flex items-center gap-2">
              <Keyboard size={14} /> Keyboard shortcuts
            </Dialog.Title>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              aria-label="Close"
            >
              <X size={14} />
            </Dialog.Close>
          </div>
          <div className="p-5 space-y-5">
            <Section title="Global" rows={GLOBAL} />
            <Section title="Universal search" rows={SEARCH} />
            <Section title="Navigation" rows={GOTO} />
            <Section title="Lists" rows={LIST} />
            <Section title="Inspector" rows={INSPECTOR} />
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Section({ title, rows }: { title: string; rows: Shortcut[] }) {
  return (
    <div>
      <h4 className="text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-2">{title}</h4>
      <div className="space-y-1">
        {rows.map((r) => (
          <div key={r.key} className="flex items-center justify-between text-sm">
            <span className="text-[var(--color-text)]">{r.label}</span>
            <kbd className="font-mono text-2xs px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] border border-[var(--color-border)] text-[var(--color-text-muted)]">
              {r.key}
            </kbd>
          </div>
        ))}
      </div>
    </div>
  );
}
