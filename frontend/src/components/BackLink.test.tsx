import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BackLink } from "./BackLink";

// A jump that left something being read offers the way back at the top of where it lands.

describe("BackLink", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("names what the entry was reached from, and goes back to it", async () => {
    history.replaceState({ loomBack: "memory api/rate-limit", graphSeed: "x" }, "", "/graph?seed=x");
    const back = vi.spyOn(history, "back").mockImplementation(() => {});
    render(<BackLink />);
    const link = screen.getByRole("button", { name: "Back to memory api/rate-limit" });
    await userEvent.click(link);
    expect(back).toHaveBeenCalledTimes(1);
  });

  it("is absent on an entry without a way back", () => {
    history.replaceState({ graphSeed: "x" }, "", "/graph?seed=x");
    render(<BackLink />);
    expect(screen.queryByTestId("back-link")).toBeNull();
  });
});
