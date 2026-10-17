import { Kbd } from "./ui";
import { Modal } from "./ui/Modal";
import { Fragment, useId, useState } from "react";
import { Switch } from "@clickhouse/click-ui/Switch";
import { setSingleKeyShortcuts, singleKeyShortcutsOn } from "../lib/shortcuts";
import { isApplePlatform, paletteShortcut } from "../lib/platform";

/** One shortcut: its keys ("g m" is a sequence, "j / ↓" two alternatives) and what it does. */
interface Shortcut {
  keys: string;
  label: string;
}

interface Group {
  title: string;
  rows: Shortcut[];
}

// The one place that lists every key. Pages show no key strips of their own; a button's
// tooltip names its key ("Accept · A").
function groups(): Group[] {
  return [
    {
      title: "Global",
      rows: [
        { keys: paletteShortcut(), label: "Focus the universal search bar" },
        { keys: "↑ / ↓", label: "Move through search results" },
        { keys: "Enter", label: "Open the search result" },
        { keys: "tag:work", label: "Search memories tagged work (partial OK)" },
        { keys: "namespace:qa", label: "Search for a namespace to jump to" },
        { keys: "?", label: "Show keyboard shortcuts" },
        { keys: "g m", label: "Go to Memories" },
        { keys: "g l / g k", label: "Go to Library" },
        { keys: "g d", label: "Go to Spaces (browse and join)" },
        { keys: "g r", label: "Go to Review queue" },
        { keys: "g a", label: "Go to Activity" },
        { keys: "g s", label: "Go to Settings" },
        { keys: "Esc", label: "Close the inspector or dialog" },
      ],
    },
    {
      title: "Memories",
      rows: [
        { keys: "/", label: "Focus the memory search" },
        { keys: "j / ↓", label: "Next memory" },
        { keys: "k / ↑", label: "Previous memory" },
        { keys: "Enter", label: "Open the memory" },
        { keys: "Space", label: "Select or unselect" },
        { keys: "e", label: "Edit the open memory's value" },
        { keys: isApplePlatform() ? "⌘Enter" : "Ctrl Enter", label: "Save the edit" },
        { keys: "Esc", label: "Cancel the edit" },
        { keys: "d", label: "Delete the open memory (asks first)" },
        { keys: "p", label: "Pin or unpin the open memory" },
      ],
    },
    {
      title: "Tasks",
      rows: [
        { keys: "j / k", label: "Next or previous task" },
        { keys: "Enter", label: "Open the task" },
        { keys: "a", label: "Accept" },
        { keys: "s", label: "Start" },
        { keys: "c", label: "Mark done" },
        { keys: "x", label: "Won't do" },
        { keys: "d", label: "Defer" },
        { keys: "/", label: "Filter by kind" },
        { keys: "Esc", label: "Close the task panel" },
      ],
    },
    {
      title: "Graph",
      rows: [
        { keys: "↑ / ↓", label: "Move through the outline" },
        { keys: "Home / End", label: "First or last row" },
        { keys: "← / →", label: "Collapse, or expand and move in" },
        { keys: "Enter", label: "Select the row's node" },
        { keys: "w", label: "Walk from here" },
        { keys: "p", label: "Find a path from the seed to here" },
        { keys: "e", label: "Expand in place" },
        { keys: "r", label: "Select the edge that reached the row" },
        { keys: "x", label: "Pin or unpin" },
      ],
    },
  ];
}

export function HelpDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const [singleKeys, setSingleKeys] = useState(singleKeyShortcutsOn);
  const describedBy = useId();
  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Keyboard shortcuts" className="max-w-[560px] w-full">
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
          d and p; and the graph outline's letters. Turn them off if speech input or a stray key sets them off;{" "}
          {paletteShortcut()}, Escape and the arrows stay on.
        </p>
      </div>
      <section
        className="max-h-[60vh] overflow-y-auto space-y-5"
        aria-label="Keyboard shortcuts"
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling region must take focus so a keyboard user can scroll it (WCAG 2.1.1)
        tabIndex={0}
      >
        {groups().map((g) => (
          <ShortcutGroup key={g.title} group={g} />
        ))}
      </section>
    </Modal>
  );
}

function ShortcutGroup({ group }: { group: Group }) {
  return (
    <div>
      <h3 className="section-label mb-2">{group.title}</h3>
      <dl className="grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-4 gap-y-1.5 items-center">
        {group.rows.map((r) => (
          <Fragment key={`${r.keys}-${r.label}`}>
            <dt className="flex flex-wrap items-center gap-1">
              <Keys keys={r.keys} />
            </dt>
            <dd className="text-sm text-text">{r.label}</dd>
          </Fragment>
        ))}
      </dl>
    </div>
  );
}

/** One cap per key: "g m" is g then m, "Ctrl K" is Ctrl with K; " / " separates alternatives. */
function Keys({ keys }: { keys: string }) {
  return keys.split(" / ").map((alt, i) => (
    <Fragment key={alt}>
      {i > 0 && <span className="text-xs text-text-muted">or</span>}
      {alt.split(" ").map((k) => (
        <Kbd key={k}>{k}</Kbd>
      ))}
    </Fragment>
  ));
}
