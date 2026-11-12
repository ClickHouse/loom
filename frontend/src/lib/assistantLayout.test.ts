import { describe, expect, it } from "vitest";
import { dockLayout, isModal, layoutOf } from "./assistantLayout";
import { useDock } from "./assistantDock";

// From md the room beside the panel is vw - sidebar - EDGE - PAGE_MIN = vw - sidebar - 640:
// `room640` computes each result from that formula.
function room640(vw: number, collapsed: boolean, pref: number) {
  if (vw < 640) return { mode: "sheet", width: vw, max: vw };
  const room = vw - (vw < 768 ? 0 : collapsed ? 60 : 244) - 640;
  const max = room >= 360 ? Math.min(720, room) : Math.max(360, Math.min(720, vw - 64));
  return { mode: room >= 360 ? "push" : "overlay", width: Math.min(max, Math.max(360, pref)), max };
}

describe("dockLayout", () => {
  it.each([
    [639, false],
    [700, false],
    [767, false],
    [768, false],
    [768, true],
    [1059, true],
    [1060, true],
    [1243, false],
    [1244, false],
    [1280, false],
    [1280, true],
    [1920, false],
  ])("at %i px (sidebar collapsed: %s) gives the room of vw - sidebar - 640", (vw, collapsed) => {
    for (const pref of [360, 420, 720]) expect(dockLayout(vw, collapsed, pref)).toEqual(room640(vw, collapsed, pref));
  });

  it("pushes at 1280 px with the sidebar open: 1280 - 244 - 48 - 592 = 396", () => {
    expect(dockLayout(1280, false, 420)).toEqual({ mode: "push", width: 396, max: 396 });
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
