import { describe, expect, it } from "vitest";
import { PAGE_MIN, PANEL_MIN, PUSH_MIN, dockLayout, isModal, layoutOf } from "./assistantLayout";
import { useDock } from "./assistantDock";

describe("dockLayout", () => {
  // [window width, sidebar collapsed, mode, largest width]: push from 1280 px while the
  // page keeps 640 px beside at least 360 px of panel, overlay below (64 px of the page in
  // view), the phone sheet below 640 px.
  it.each([
    [639, false, "sheet", 639],
    [640, false, "overlay", 576],
    [640, true, "overlay", 576],
    [1279, false, "overlay", 720],
    [1279, true, "overlay", 720],
    [1280, false, "push", 396],
    [1280, true, "push", 580],
    [1440, false, "push", 556],
    [1440, true, "push", 720],
    [1920, false, "push", 720],
  ] as const)("at %i px (sidebar collapsed: %s) is %s, at most %i px wide", (vw, collapsed, mode, max) => {
    const layout = dockLayout(vw, collapsed, 420);
    expect(layout.mode).toBe(mode);
    expect(layout.max).toBe(max);
    expect(layout.width).toBe(mode === "sheet" ? vw : Math.min(max, 420));
  });

  it("leaves room for the page and the narrowest panel at PUSH_MIN with the sidebar open", () => {
    expect(PUSH_MIN - 244 - PAGE_MIN).toBeGreaterThanOrEqual(PANEL_MIN);
  });

  it("keeps a stored width within the room", () => {
    expect(dockLayout(1280, false, 720)).toEqual({ mode: "push", width: 396, max: 396 });
    expect(dockLayout(1440, true, 300)).toEqual({ mode: "push", width: 360, max: 720 });
  });
});

describe("the expanded window", () => {
  const base = { ...useDock.getState(), viewport: 1440, collapsed: false, prefWidth: 420 };
  it("is the layout only while open, and is modal", () => {
    expect(layoutOf({ ...base, open: true, expanded: true }).mode).toBe("expanded");
    expect(layoutOf({ ...base, open: false, expanded: true }).mode).toBe("push");
    expect(isModal({ ...base, open: true, expanded: true })).toBe(true);
    expect(isModal({ ...base, open: true, expanded: false })).toBe(false);
  });
});
