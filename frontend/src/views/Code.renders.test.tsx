import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { CodeView } from "./Code";
import { codeRepoFixture, stubCodeFetch } from "../test/codeRepos";

// The cards are memoised and filter by the text a render behind the box: a keystroke that
// leaves the cards as they were renders none of them again. Each card is a ListRow, so
// ListRow counts the cards rendered.

const renders = { count: 0 };
vi.mock("../components/ui/ListRow", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../components/ui/ListRow")>();
  return {
    ...actual,
    ListRow: (props: Parameters<typeof actual.ListRow>[0]) => {
      renders.count += 1;
      return <actual.ListRow {...props} />;
    },
  };
});

const REPOS = ["acme/app", "acme/api", "acme/web"].map((repo, i) => ({
  ...codeRepoFixture,
  repo,
  namespace: `code-${i}`,
}));

describe("Repositories card renders", () => {
  beforeEach(() => {
    renders.count = 0;
    invalidateShared();
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "default", agent: "loom-ui" }, view: "code" });
    stubCodeFetch(() => REPOS);
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("a keystroke whose filter every card still matches renders no card again", async () => {
    render(<CodeView />);
    await screen.findByText("acme/web", {}, { timeout: 2000 });
    renders.count = 0;
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Filter repositories"), { target: { value: "acme" } });
    });
    expect(screen.getByText("acme/web")).toBeInTheDocument();
    expect(renders.count).toBe(0);
  });
});
