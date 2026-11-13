import { act, cleanup, fireEvent, render, screen, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useDock } from "../lib/assistantDock";
import { PANEL_DEFAULT } from "../lib/assistantLayout";
import { expandAssistant, installDock, openAssistant } from "../lib/assistantDockRuntime";
import { useStore } from "../lib/state";
import { AssistantPanel } from "./AssistantPanel";

// The panel's own element: the window it expands to, its splitter, and its slide-in, which
// plays only when it appears from closed over the page.

vi.mock("../views/assistant/Dock", () => ({
  DockBody: () => (
    <div>
      <textarea aria-label="Question" />
      <span data-testid="child" />
    </div>
  ),
}));

beforeAll(() => {
  window.innerWidth = 1440;
  installDock();
});

beforeEach(() => {
  window.innerWidth = 1440;
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: /min-width: (\d+)px/.test(query) && window.innerWidth >= Number(/(\d+)px/.exec(query)?.[1]),
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
  useDock.setState({
    open: false,
    mounted: true,
    expanded: false,
    expandedFromPush: false,
    entering: false,
    viewport: 1440,
    collapsed: false,
    prefWidth: PANEL_DEFAULT,
    focusField: false,
    history: false,
  });
  useStore.setState({ view: "memories", identity: { org: "dev", namespace: "team", agent: "loom-ui" } });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useDock.setState({ expanded: false, open: false });
  window.innerWidth = 1024;
});

const panel = () => document.querySelector("[data-assistant-panel]") as HTMLElement;

describe("the slide-in", () => {
  it("does not play for an open beside the page, nor for a resize to overlay or an Expand after it", async () => {
    render(<AssistantPanel />);
    act(() => openAssistant());
    expect(panel()).not.toHaveAttribute("data-entering");
    act(() => useDock.setState({ viewport: 900 }));
    expect(panel()).toHaveAttribute("data-mode", "overlay");
    expect(panel()).not.toHaveAttribute("data-entering");
    act(() => useDock.setState({ viewport: 1440 }));
    act(() => expandAssistant());
    expect(panel()).toHaveAttribute("data-mode", "expanded");
    expect(panel()).not.toHaveAttribute("data-entering");
  });

  it("plays for an open over the page, until the panel's own animation ends", () => {
    useDock.setState({ viewport: 900 });
    render(<AssistantPanel />);
    act(() => openAssistant());
    expect(panel()).toHaveAttribute("data-entering");
    // A child's animation is not the panel's.
    fireEvent.animationEnd(screen.getByTestId("child"));
    expect(panel()).toHaveAttribute("data-entering");
    fireEvent.animationEnd(panel());
    expect(panel()).not.toHaveAttribute("data-entering");
  });

  it("the window opened from closed plays it", () => {
    render(<AssistantPanel />);
    act(() => openAssistant({ expanded: true }));
    expect(panel()).toHaveAttribute("data-mode", "expanded");
    expect(panel()).toHaveAttribute("data-entering");
  });
});

describe("the window", () => {
  it("has a level-2 title, Expand and Collapse are one button, and from closed there is no spacer", async () => {
    render(<AssistantPanel />);
    act(() => openAssistant({ expanded: true }));
    expect(screen.getByRole("heading", { level: 2, name: "Ask AI" })).toHaveAttribute("id", "assistant-panel-title");
    expect(document.querySelector("[data-assistant-spacer]")).toBeNull();
    const button = screen.getByRole("button", { name: "Collapse" });
    await userEvent.click(button);
    expect(button).toHaveAccessibleName("Expand");
    expect(button).toHaveFocus();
    expect(useDock.getState().expanded).toBe(false);
  });

  it("is offered from 1024 px only", () => {
    window.innerWidth = 900;
    useDock.setState({ viewport: 900 });
    render(<AssistantPanel />);
    act(() => openAssistant({ expanded: true }));
    expect(useDock.getState().expanded).toBe(false);
    expect(screen.queryByRole("button", { name: "Expand" })).toBeNull();
  });
});

describe("the header", () => {
  it("is one row: the title, the namespace muted, then New chat, History, Expand and Close, always there", async () => {
    render(<AssistantPanel />);
    act(() => openAssistant());
    expect(panel()).toHaveAccessibleName("Ask AI");
    const head = panel().querySelector(".assistant-head") as HTMLElement;
    expect(within(head).getByRole("heading", { level: 2 })).toHaveTextContent("Ask AI");
    // The namespace in muted text, with no prefix and no link.
    const ns = within(head).getByTestId("assistant-namespace");
    expect(ns).toHaveTextContent(/^team$/);
    expect(ns).toHaveClass("text-text-muted");
    expect(within(head).queryByRole("link")).toBeNull();
    expect(
      within(head)
        .getAllByRole("button")
        .map((b) => b.getAttribute("aria-label")),
    ).toEqual(["New chat", "History", "Expand", "Close"]);
  });

  // The "+" sits near the top bar's New, so each icon says its action in a visible tooltip.
  it.each(["New chat", "History", "Expand", "Close"])("%s shows its name in a tooltip", async (name) => {
    render(<AssistantPanel />);
    act(() => openAssistant());
    const user = userEvent.setup();
    await user.hover(screen.getByRole("button", { name }));
    expect(await screen.findByRole("tooltip")).toHaveTextContent(new RegExp(`^${name}$`));
  });

  it("History toggles the panel's History view, pressed while it shows, with Back and its title", async () => {
    render(<AssistantPanel />);
    act(() => openAssistant());
    const history = screen.getByRole("button", { name: "History" });
    expect(history).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(history);
    expect(useDock.getState().history).toBe(true);
    expect(history).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("History");
    expect(screen.queryByTestId("assistant-namespace")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Back to the chat" }));
    expect(useDock.getState()).toMatchObject({ history: false, focusField: true });
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Ask AI");
  });

  it("New chat asks the body for a new chat, and is unavailable while an answer streams", async () => {
    render(<AssistantPanel />);
    act(() => openAssistant());
    const before = useDock.getState().newChatSeq ?? 0;
    const button = screen.getByRole("button", { name: "New chat" });
    await userEvent.click(button);
    expect(useDock.getState().newChatSeq).toBe(before + 1);
    act(() => useDock.setState({ working: true }));
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAccessibleDescription("An answer is still coming in.");
    await userEvent.click(button);
    expect(useDock.getState().newChatSeq).toBe(before + 1);
    act(() => useDock.setState({ working: false }));
  });

  it("Close closes the panel", async () => {
    render(<AssistantPanel />);
    act(() => openAssistant());
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(useDock.getState().open).toBe(false);
  });
});

describe("the splitter", () => {
  it("measures the width from the panel's own right edge", () => {
    render(<AssistantPanel />);
    act(() => openAssistant());
    // jsdom lays nothing out: the panel's right edge is given, away from the window's.
    vi.spyOn(panel(), "getBoundingClientRect").mockReturnValue({ right: 1392 } as DOMRect);
    const splitter = screen.getByRole("separator", { name: "Resize Ask AI" });
    fireEvent.pointerDown(splitter, { pointerId: 1, clientX: 972 });
    fireEvent.pointerMove(splitter, { pointerId: 1, clientX: 900 });
    fireEvent.pointerUp(splitter, { pointerId: 1 });
    expect(useDock.getState().prefWidth).toBe(492);
  });
});
