import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { arriveAt, readBackLabel, readPlace, rememberPlace, restorePlace } from "./place";

// Back restores the place: the scroll of <main>, the focused element and the open
// memory are kept in the history entry being left; a new view's h1 takes the focus and one
// polite status says where the navigation went.

function page(html: string) {
  document.body.innerHTML = `<main id="main">${html}</main><p id="loom-nav-status" role="status"></p>`;
  return document.getElementById("main")!;
}

function tall(main: HTMLElement, height: number) {
  Object.defineProperty(main, "scrollHeight", { value: height, configurable: true });
  Object.defineProperty(main, "clientHeight", { value: 500, configurable: true });
}

describe("rememberPlace / readPlace", () => {
  beforeEach(() => history.replaceState({ graphSeed: "x" }, "", "/memories"));

  it("writes the place into the current entry, keeping what else it holds", () => {
    const main = page('<div data-focus-key="memory:a"><button>open a</button></div>');
    main.scrollTop = 300;
    main.querySelector("button")!.focus();
    rememberPlace({ key: "a", agent: "bot" });
    expect(history.state.graphSeed).toBe("x");
    expect(readPlace(history.state)).toEqual({
      scrollTop: 300,
      focusKey: "memory:a",
      inspected: "a",
      inspectedAgent: "bot",
      inspectedOwner: null,
    });
  });

  it("keeps the user and session an inspected row is filed under", () => {
    rememberPlace({ key: "a", agent: "bot", owner: { user_id: "ada", session_id: "-" } });
    expect(readPlace(history.state)?.inspectedOwner).toEqual({ user_id: "ada", session_id: "-" });
    rememberPlace({ key: "a", agent: "bot" });
    expect(readPlace(history.state)?.inspectedOwner).toBeNull();
  });

  it("an explicit focus key wins over the focused element", () => {
    page("<button>popover link</button>");
    rememberPlace({ key: null, agent: "" }, "entity:redis");
    expect(readPlace(history.state)?.focusKey).toBe("entity:redis");
  });

  it("reads nothing from an entry without a place or a way back", () => {
    expect(readPlace(null)).toBeNull();
    expect(readPlace({ graphSeed: "x" })).toBeNull();
    expect(readBackLabel({ loomBack: "memory k" })).toBe("memory k");
    expect(readBackLabel(null)).toBeNull();
  });
});

describe("restorePlace and arriveAt", () => {
  beforeEach(() =>
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "setTimeout", "Date"] }),
  );
  afterEach(() => vi.useRealTimers());

  it("scrolls back once the page is tall enough, then focuses the keyed element", () => {
    const main = page("<h1>Memories</h1>");
    tall(main, 600);
    restorePlace(
      { scrollTop: 900, focusKey: "entity:redis", inspected: "k", inspectedAgent: "", inspectedOwner: null },
      true,
    );
    vi.advanceTimersByTime(20);
    expect(main.scrollTop).toBe(0);
    // The list and the drawer arrive.
    tall(main, 2000);
    main.insertAdjacentHTML("beforeend", '<a href="/graph?seed=redis" data-focus-key="entity:redis">redis</a>');
    vi.advanceTimersByTime(50);
    expect(main.scrollTop).toBe(900);
    expect(document.activeElement).toBe(main.querySelector("a"));
    vi.advanceTimersByTime(100);
    expect(document.getElementById("loom-nav-status")!.textContent).toBe("Memories");
  });

  it("restores the offset again when deferred rows move it after focus", () => {
    const main = page('<h1>Memories</h1><a href="/graph" data-focus-key="entity:redis">redis</a>');
    tall(main, 2000);
    main.querySelector("a")!.addEventListener("focus", () => {
      requestAnimationFrame(() => {
        main.scrollTop = 770;
      });
    });
    restorePlace(
      { scrollTop: 900, focusKey: "entity:redis", inspected: "k", inspectedAgent: "", inspectedOwner: null },
      true,
    );
    vi.advanceTimersByTime(20);
    expect(main.scrollTop).toBe(900);
    vi.advanceTimersByTime(20);
    expect(main.scrollTop).toBe(900);
    vi.advanceTimersByTime(20);
    main.scrollTop = 600;
    vi.advanceTimersByTime(100);
    expect(main.scrollTop).toBe(600);
  });

  it("restores the top of the page from a scrolled view", () => {
    const main = page("<h1>Memories</h1>");
    tall(main, 2000);
    main.scrollTop = 300;
    restorePlace({ scrollTop: 0, focusKey: null, inspected: null, inspectedAgent: "", inspectedOwner: null }, true);
    vi.advanceTimersByTime(40);
    expect(main.scrollTop).toBe(0);
  });

  it("stops retrying an unreachable offset at the scroll limit", () => {
    const main = page("<h1>Memories</h1>");
    tall(main, 600);
    let offset = 0;
    Object.defineProperty(main, "scrollTop", {
      get: () => offset,
      set: (value: number) => {
        offset = Math.min(value, 100);
      },
    });
    restorePlace({ scrollTop: 900, focusKey: null, inspected: null, inspectedAgent: "", inspectedOwner: null }, true);
    vi.advanceTimersByTime(3100);
    expect(main.scrollTop).toBe(100);
    main.scrollTop = 50;
    vi.advanceTimersByTime(100);
    expect(main.scrollTop).toBe(50);
  });

  it("a new navigation cancels the pending scroll verification", () => {
    const main = page("<h1>Memories</h1>");
    tall(main, 2000);
    restorePlace({ scrollTop: 900, focusKey: null, inspected: null, inspectedAgent: "", inspectedOwner: null }, true);
    vi.advanceTimersByTime(20);
    arriveAt();
    main.scrollTop = 300;
    vi.advanceTimersByTime(100);
    expect(main.scrollTop).toBe(300);
  });

  // Back to Memories focuses the row, not its first focusable control (the checkbox).
  it("a keyed list row gives the focus to its open button, not its checkbox", () => {
    const main = page(
      '<h1>Memories</h1><div class="list-row" data-focus-key="memory:a">' +
        '<button role="checkbox" aria-label="Select a"></button><button data-row-open="">a</button></div>',
    );
    tall(main, 2000);
    restorePlace(
      { scrollTop: 0, focusKey: "memory:a", inspected: null, inspectedAgent: "", inspectedOwner: null },
      true,
    );
    vi.advanceTimersByTime(20);
    expect(document.activeElement).toBe(main.querySelector("[data-row-open]"));
  });

  it("without a focus key, a changed view focuses its h1", () => {
    const main = page("<h1>Library</h1>");
    tall(main, 2000);
    restorePlace({ scrollTop: 0, focusKey: null, inspected: null, inspectedAgent: "", inspectedOwner: null }, true);
    vi.advanceTimersByTime(20);
    expect(document.activeElement).toBe(main.querySelector("h1"));
    expect(main.querySelector("h1")!.getAttribute("tabindex")).toBe("-1");
  });

  it("a new view focuses its h1 once drawn and says the note", () => {
    const main = page("");
    arriveAt("Graph, walked from redis");
    main.insertAdjacentHTML("beforeend", "<h1>Graph</h1>");
    vi.advanceTimersByTime(100);
    expect(document.activeElement).toBe(main.querySelector("h1"));
    expect(document.getElementById("loom-nav-status")!.textContent).toBe("Graph, walked from redis");
  });

  it("an editor the navigation opened keeps the focus", () => {
    const main = page('<h1>Profile</h1><div role="dialog"><input aria-label="Section" /></div>');
    main.querySelector("input")!.focus();
    arriveAt();
    vi.advanceTimersByTime(100);
    expect(document.activeElement).toBe(main.querySelector("input"));
    expect(document.getElementById("loom-nav-status")!.textContent).toBe("Profile");
  });
});
