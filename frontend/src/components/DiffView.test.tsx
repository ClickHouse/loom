import { cleanup, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it } from "vitest";
import { DiffView } from "./DiffView";

// The memory history panel's line diff: what it shows for equal versions, the marker and
// order of each changed line, the two labels, and an empty version on either side.

afterEach(cleanup);

/** Each diff line as it reads: its marker, then its text. */
function lines(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll(".whitespace-pre-wrap > div"), (l) => l.textContent ?? "");
}

describe("DiffView", () => {
  it("says there is no difference when the versions are equal, and draws no diff", () => {
    const { container } = render(<DiffView prev={"a\nb"} next={"a\nb"} prevLabel="v1" nextLabel="v2" />);
    expect(screen.getByText("No textual difference between these versions.")).toBeTruthy();
    expect(screen.queryByText("− v1")).toBeNull();
    expect(lines(container)).toEqual([]);
  });

  it("marks a removed line with − and an added one with +, in the order they occur", () => {
    const { container } = render(
      <DiffView prev={"keep\nold\ntail"} next={"keep\nnew\ntail\nend"} prevLabel="v1" nextLabel="v2" />,
    );
    expect(lines(container)).toEqual([" keep", "−old", "+new", " tail", "+end"]);
  });

  it("names both versions in its header", () => {
    render(<DiffView prev="before" next="after" prevLabel="version 3" nextLabel="version 4" />);
    expect(screen.getByText("− version 3")).toBeTruthy();
    expect(screen.getByText("+ version 4")).toBeTruthy();
  });

  it("renders an empty version on either side", () => {
    const added = render(<DiffView prev="" next={"one\ntwo"} prevLabel="empty" nextLabel="filled" />);
    expect(lines(added.container).filter((l) => l.startsWith("+"))).toEqual(["+one", "+two"]);
    expect(screen.getByText("− empty")).toBeTruthy();
    cleanup();
    const removed = render(<DiffView prev={"one\ntwo"} next="" prevLabel="filled" nextLabel="empty" />);
    expect(lines(removed.container).filter((l) => l.startsWith("−"))).toEqual(["−one", "−two"]);
    expect(screen.getByText("+ empty")).toBeTruthy();
  });
});
