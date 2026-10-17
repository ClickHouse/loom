import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { useStore } from "../lib/state";
import { MemoryScopeMissing } from "./MemoryScopeMissing";

// A tracker namespace's memory page jumps to its Patterns board as a new history entry, so
// Back returns to the page the jump was made from.

describe("MemoryScopeMissing", () => {
  afterEach(() => cleanup());

  it("Open in Patterns pushes /graph?mode=patterns and keeps the origin's URL as it was", async () => {
    history.replaceState(null, "", "/memories");
    useStore.setState({
      identity: { org: "dev", namespace: "tracker-x", agent: "loom-ui" },
      myNamespaces: [
        {
          namespace: "tracker-x",
          role: "viewer",
          status: "active",
          visibility: "private",
          description: "",
          joined_at: null,
          is_new: false,
          kind: "qa",
        },
      ],
      view: "memories",
    });
    render(<MemoryScopeMissing view="memories" />);
    const before = history.length;
    await userEvent.click(screen.getByRole("button", { name: "Open in Patterns" }));
    expect(history.length).toBe(before + 1);
    expect(location.pathname + location.search).toBe("/graph?mode=patterns");
    expect(useStore.getState().view).toBe("graph");
  });
});
