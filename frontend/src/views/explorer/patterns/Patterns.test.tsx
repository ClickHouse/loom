import { cleanup, render, screen, waitFor, within } from "../../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Patterns } from "./Patterns";

// A window whose board read fails says so, and keeps none of the previous window's board:
// neither its cells nor the components the facets list from it.

const trackerAnswer = vi.hoisted(() => vi.fn());
vi.mock("../../../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api")>()),
  trackerAnswer: (...a: unknown[]) => trackerAnswer(...a),
  trackerItem: vi.fn(async () => null),
}));

const answer = (template: string, rows: Record<string, unknown>[]) => ({
  intent: "",
  anchors: {},
  sections: [{ template, title: "", params: {}, status: "served", rows }],
  searched: [],
  lines: [],
  restricted: false,
  verdict: "answered",
});

afterEach(() => {
  cleanup();
  trackerAnswer.mockReset();
});

describe("Patterns", () => {
  it("a failed board read for another window keeps none of the last window's board", async () => {
    trackerAnswer.mockImplementation(async (_t: unknown, opts: { question: string; anchors: { days?: number } }) => {
      if (opts.question === "bug board" && opts.anchors.days === 90) throw new Error("down");
      return opts.question === "bug board"
        ? answer("severity_board", [
            { component: "MergeTree", severity: "low", open: 3, untested: 1, flaky: 0, bisected: 0 },
          ])
        : answer("test_gaps", []);
    });
    render(
      <Patterns
        identity={{ org: "dev", namespace: "tracker", agent: "loom-ui" }}
        binding={{ tracker_namespace: "tracker", restricted: false, kind: "tracker", authorized: true } as never}
        isEntity={() => false}
        onWalk={() => {}}
        initial={{ component: null, severity: null, issue: null }}
        onUrlState={() => {}}
      />,
    );
    expect((await screen.findAllByText("MergeTree", {}, { timeout: 2000 })).length).toBeGreaterThan(0);
    await userEvent.click(
      within(screen.getByRole("group", { name: "Window" })).getByRole("button", { name: "90 days" }),
    );
    await waitFor(() => expect(screen.queryAllByText("MergeTree")).toHaveLength(0), { timeout: 2000 });
  });
});
