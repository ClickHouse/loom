import { Kbd } from "./ui";
import { Modal } from "./ui/Modal";
import { useId, useState } from "react";
import { Switch } from "@clickhouse/click-ui/Switch";
import { setSingleKeyShortcuts, singleKeyShortcutsOn } from "../lib/shortcuts";

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
  { key: "g s", label: "Go to Settings" },
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
  const [singleKeys, setSingleKeys] = useState(singleKeyShortcutsOn);
  const describedBy = useId();
  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Keyboard shortcuts" className="max-w-[520px] w-full">
      <div className="mb-4 space-y-1">
        <Switch
          checked={singleKeys}
          onCheckedChange={(on) => {
            setSingleKeyShortcuts(on);
            setSingleKeys(on);
          }}
          label="Single-key shortcuts"
          orientation="horizontal"
          dir="start"
          aria-describedby={describedBy}
        />
        <p id={describedBy} className="text-xs text-text-muted">
          Every one-key shortcut: ?, /, g then a letter, j and k; the Tasks keys (a, s, c, x, d); the memory drawer's e,
          d and p; and the graph outline's letters. Turn them off if speech input or a stray key sets them off; ⌘K,
          Escape and the arrows stay on.
        </p>
      </div>
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
