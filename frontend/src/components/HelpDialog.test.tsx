import { fireEvent, render, screen, within } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { singleKeyShortcutsOn } from "../lib/shortcuts";
import { HelpDialog } from "./HelpDialog";

// The help dialog is the one place that lists every key, grouped by area, each group a
// list of keys and what they do.
describe("HelpDialog", () => {
  afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("names the Settings page", () => {
    render(<HelpDialog open onOpenChange={() => {}} />);
    expect(screen.getByText("Go to Settings")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).not.toHaveTextContent(/Setup/);
  });

  it("groups the keys as Global, Memories, Tasks and Graph, the switch first", () => {
    render(<HelpDialog open onOpenChange={() => {}} />);
    const dialog = screen.getByRole("dialog");
    const headings = within(dialog).getAllByRole("heading", { level: 3 });
    expect(headings.map((h) => h.textContent)).toEqual(["Global", "Memories", "Tasks", "Graph"]);
    // The single-key switch comes before every group (WCAG 2.1.4).
    const toggle = within(dialog).getByRole("switch", { name: "Single-key shortcuts" });
    expect(toggle.compareDocumentPosition(headings[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Each group is a description list: a key term, then what it does.
    const lists = dialog.querySelectorAll("dl");
    expect(lists).toHaveLength(4);
    const tasks = lists[2];
    const accept = [...tasks.querySelectorAll("dd")].find((d) => d.textContent === "Accept");
    expect(accept?.previousElementSibling?.tagName).toBe("DT");
    expect(accept?.previousElementSibling).toHaveTextContent("a");
    // The graph outline's letters live here now, not under the outline.
    expect(within(lists[3] as HTMLElement).getByText("Walk from here")).toBeInTheDocument();
  });

  it("labels the search shortcut for the platform", () => {
    vi.stubGlobal("navigator", { ...navigator, platform: "Win32" });
    const { unmount } = render(<HelpDialog open onOpenChange={() => {}} />);
    const first = screen.getAllByRole("term")[0];
    expect([...first.querySelectorAll("kbd")].map((k) => k.textContent)).toEqual(["Ctrl", "K"]);
    unmount();
    vi.stubGlobal("navigator", { ...navigator, platform: "MacIntel" });
    render(<HelpDialog open onOpenChange={() => {}} />);
    expect(screen.getAllByRole("term")[0]).toHaveTextContent("⌘K");
  });

  // WCAG 2.1.4: a shortcut of one character key can be turned off.
  it("turns the single-key shortcuts off and on", () => {
    render(<HelpDialog open onOpenChange={() => {}} />);
    const toggle = screen.getByRole("switch", { name: "Single-key shortcuts" });
    expect(toggle).toBeChecked();
    // Its description names every key it turns off: the global ones, the Tasks keys and the
    // memory drawer's.
    expect(toggle).toHaveAccessibleDescription(/Tasks/);
    expect(toggle).toHaveAccessibleDescription(/e, d and p/);
    fireEvent.click(toggle);
    expect(toggle).not.toBeChecked();
    expect(singleKeyShortcutsOn()).toBe(false);
    fireEvent.click(toggle);
    expect(singleKeyShortcutsOn()).toBe(true);
  });
});
