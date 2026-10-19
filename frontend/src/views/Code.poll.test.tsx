/**
 * The Repositories list polls every two minutes. A poll that fails after a good read keeps
 * the cards and says it could not refresh; only a failed first read replaces them.
 */
import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { CodeView } from "./Code";
import { codeRepoFixture, stubCodeFetch } from "../test/codeRepos";

describe("Repositories, a failed poll", () => {
  let reposFail = false;
  beforeEach(() => {
    invalidateShared();
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "default", agent: "loom-ui" }, view: "code" });
    stubCodeFetch(() => (reposFail ? null : [codeRepoFixture]));
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    reposFail = false;
  });

  it("keeps the cards and says it could not refresh", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<CodeView />);
    expect((await screen.findAllByText("acme/app", {}, { timeout: 2000 })).length).toBeGreaterThan(0);
    reposFail = true;
    await act(async () => {
      vi.advanceTimersByTime(120_000);
    });
    await waitFor(() => expect(screen.getByText(/Couldn't refresh the repositories/)).toBeInTheDocument());
    expect(screen.getAllByText("acme/app").length).toBeGreaterThan(0);
    expect(screen.queryByText("Couldn't load repositories")).toBeNull();
  });
});
