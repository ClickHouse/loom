import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDock } from "../lib/assistantDock";
import { PANEL_DEFAULT } from "../lib/assistantLayout";
import { useStore } from "../lib/state";
import { AssistantPanel } from "./AssistantPanel";

// /assistant is the assistant panel itself, expanded in <main>'s place: Expand on the
// docked panel goes there, Collapse comes back (Back when the app made the entry, else
// Memories in its place) with the panel open and its field focused.

vi.mock("../views/assistant/Dock", () => ({
  DockBody: () => <textarea aria-label="Question" />,
}));

beforeEach(() => {
  useDock.setState({
    open: true,
    mounted: true,
    expanded: false,
    viewport: 1440,
    collapsed: false,
    prefWidth: PANEL_DEFAULT,
    focusField: false,
  });
  useStore.setState({ view: "memories", identity: { org: "dev", namespace: "team", agent: "loom-ui" } });
});
afterEach(() => {
  cleanup();
  useDock.setState({ expanded: false, open: false });
  window.history.replaceState(null, "", "/");
});

describe("the expanded assistant", () => {
  it("Expand goes to /assistant from the docked panel", async () => {
    const navigate = vi.fn();
    useStore.setState({ navigate });
    render(<AssistantPanel />);
    expect(screen.getByRole("separator", { name: "Resize the assistant panel" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Expand the assistant" }));
    expect(navigate).toHaveBeenCalledWith("assistant");
  });

  it("is the page: an h1, no splitter, shown whatever the panel's open state, and Esc closes nothing", async () => {
    useDock.setState({ expanded: true, open: false });
    render(<AssistantPanel />);
    const panel = document.querySelector("[data-assistant-panel]") as HTMLElement;
    expect(panel).toHaveAttribute("data-mode", "expanded");
    expect(panel).not.toHaveAttribute("hidden");
    expect(panel).toHaveAttribute("role", "region");
    expect(screen.getByRole("heading", { level: 1, name: "Assistant" })).toBeInTheDocument();
    expect(screen.queryByRole("separator")).toBeNull();
    expect(screen.queryByRole("button", { name: "Expand the assistant" })).toBeNull();
    await userEvent.keyboard("{Escape}");
    expect(useDock.getState().expanded).toBe(true);
  });

  it("Collapse from a deep link lands on Memories in its place, the panel open", async () => {
    window.history.replaceState(null, "", "/assistant");
    useStore.setState({ view: "assistant" });
    useDock.setState({ expanded: true, open: false });
    const before = window.history.length;
    render(<AssistantPanel />);
    await userEvent.click(screen.getByRole("button", { name: "Collapse the assistant" }));
    await waitFor(() => expect(useStore.getState().view).toBe("memories"));
    expect(window.location.pathname).toBe("/memories");
    expect(window.history.length).toBe(before);
    expect(useDock.getState()).toMatchObject({ open: true, focusField: true });
  });

  it("Collapse on a phone opens the sheet with its own history entry, so Back closes only the sheet", async () => {
    window.history.replaceState(null, "", "/assistant");
    useStore.setState({ view: "assistant" });
    useDock.setState({ expanded: true, open: false, viewport: 375 });
    const before = window.history.length;
    render(<AssistantPanel />);
    await userEvent.click(screen.getByRole("button", { name: "Collapse the assistant" }));
    await waitFor(() => expect(useStore.getState().view).toBe("memories"));
    expect(useDock.getState()).toMatchObject({ open: true, expanded: false, focusField: true });
    // Memories replaced /assistant, and the sheet pushed its entry on top of it.
    expect(window.history.length).toBe(before + 1);
    expect(window.history.state).toMatchObject({ loomSheet: true });
    expect(window.location.pathname).toBe("/memories");
  });

  it("Collapse after Expand goes Back", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    window.history.replaceState({ loomFromApp: true }, "", "/assistant");
    useDock.setState({ expanded: true });
    render(<AssistantPanel />);
    await userEvent.click(screen.getByRole("button", { name: "Collapse the assistant" }));
    expect(back).toHaveBeenCalledTimes(1);
    act(() => back.mockRestore());
  });
});
