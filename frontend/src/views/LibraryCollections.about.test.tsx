// What the place shown is, behind the place line's info button. In a file of its own: the
// hover a real click makes leaves React giving later updates a pointer event's priority, which
// splits the Library's create reads in the specs that follow it in one file.
import { afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { cleanup, render, screen } from "../test/rtl";
import { Picker, read, ROWS } from "../test/libraryCollections";

describe("About this place", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("says what a viewer's collection is and who reaches it, on a press", async () => {
    render(<Picker org="acme" namespace="team" collections={read(ROWS)} current={ROWS[0]} onPick={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "About this place" }));
    expect(await screen.findByText(/^Viewer of a collection: .*everyone who reaches col-q3/)).toBeInTheDocument();
  });
});
