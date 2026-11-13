import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
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
    expect(screen.getByRole("heading", { level: 2, name: "Assistant" })).toHaveAttribute("id", "assistant-panel-title");
    expect(document.querySelector("[data-assistant-spacer]")).toBeNull();
    const button = screen.getByRole("button", { name: "Collapse the assistant" });
    await userEvent.click(button);
    expect(button).toHaveAccessibleName("Expand the assistant");
    expect(button).toHaveFocus();
    expect(useDock.getState().expanded).toBe(false);
  });

  it("is offered from 1024 px only", () => {
    window.innerWidth = 900;
    useDock.setState({ viewport: 900 });
    render(<AssistantPanel />);
    act(() => openAssistant({ expanded: true }));
    expect(useDock.getState().expanded).toBe(false);
    expect(screen.queryByRole("button", { name: "Expand the assistant" })).toBeNull();
  });
});

describe("the splitter", () => {
  it("measures the width from the panel's own right edge", () => {
    render(<AssistantPanel />);
    act(() => openAssistant());
    // jsdom lays nothing out: the panel's right edge is given, away from the window's.
    vi.spyOn(panel(), "getBoundingClientRect").mockReturnValue({ right: 1392 } as DOMRect);
    const splitter = screen.getByRole("separator", { name: "Resize the assistant panel" });
    fireEvent.pointerDown(splitter, { pointerId: 1, clientX: 972 });
    fireEvent.pointerMove(splitter, { pointerId: 1, clientX: 900 });
    fireEvent.pointerUp(splitter, { pointerId: 1 });
    expect(useDock.getState().prefWidth).toBe(492);
  });
});
