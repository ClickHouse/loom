import { Kbd } from "./ui";
import { Modal } from "./ui/Modal";

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
    <Modal open={open} onOpenChange={onOpenChange} title="Keyboard shortcuts" className="max-w-[520px] w-full">
      <section
        className="max-h-[60vh] overflow-y-auto space-y-5"
        aria-label="Keyboard shortcuts"
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling region must take focus so a keyboard user can scroll it (WCAG 2.1.1)
        tabIndex={0}
      >
        <Section title="Global" rows={GLOBAL} />
        <Section title="Universal search" rows={SEARCH} />
        <Section title="Navigation" rows={GOTO} />
        <Section title="Lists" rows={LIST} />
        <Section title="Inspector" rows={INSPECTOR} />
      </section>
    </Modal>
  );
}

function Section({ title, rows }: { title: string; rows: Shortcut[] }) {
  return (
    <div>
      <h3 className="section-label mb-2">{title}</h3>
      <div className="space-y-1">
        {rows.map((r) => (
          <div key={r.key} className="flex items-center justify-between text-sm">
            <span className="text-text">{r.label}</span>
            <Kbd>{r.key}</Kbd>
          </div>
        ))}
      </div>
    </div>
  );
}
