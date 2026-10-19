/**
 * The Repositories filter: a filter that matches nothing says so, and Clear filters empties
 * the box and brings the cards back.
 */
import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { CodeView } from "./Code";
import { codeRepoFixture, stubCodeFetch } from "../test/codeRepos";

describe("Repositories, the filter", () => {
  beforeEach(() => {
    invalidateShared();
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "default", agent: "loom-ui" }, view: "code" });
    stubCodeFetch(() => [codeRepoFixture]);
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("Clear filters on a filter that matches nothing empties the box and brings the cards back", async () => {
    render(<CodeView />);
    expect((await screen.findAllByText("acme/app", {}, { timeout: 2000 })).length).toBeGreaterThan(0);
    const box = screen.getByLabelText("Filter repositories");
    await userEvent.type(box, "nothing");
    await userEvent.click(await screen.findByRole("button", { name: "Clear filters" }));
    expect(box).toHaveValue("");
    await waitFor(() => expect(screen.getAllByText("acme/app").length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });
});
