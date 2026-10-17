import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "../../test/rtl";
import { ListTime } from "./ListTime";

// A row's time reads as how long ago, and the absolute UTC time is one hover or focus away.
describe("ListTime", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-17T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("renders a <time> with its machine time and the absolute UTC time in a tooltip", async () => {
    render(<ListTime iso="2026-09-20 14:32:00" />);
    const time = screen.getByText("Sep 20");
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("dateTime", "2026-09-20T14:32:00.000Z");
    expect(time).toHaveAttribute("tabindex", "0");
    act(() => time.focus());
    fireEvent.focus(time);
    expect(await screen.findAllByText("Sep 20, 2026, 14:32 UTC")).not.toHaveLength(0);
  });

  it("takes no tab stop inside a row's button", () => {
    render(
      <button type="button">
        open <ListTime iso="2026-10-17T11:48:00Z" />
      </button>,
    );
    expect(screen.getByText("12m ago")).not.toHaveAttribute("tabindex");
  });

  // Review findings: a time in a table or grid row took its own tab stop (fifty per page of
  // runs); there it is quiet, and a screen reader still hears the absolute time.
  it("takes no tab stop in a table or grid row, and says the absolute time to a screen reader", () => {
    render(
      <table>
        <tbody>
          <tr>
            <td>
              <ListTime iso="2026-09-20 14:32:00" />
            </td>
          </tr>
        </tbody>
      </table>,
    );
    const time = screen.getByText("Sep 20");
    expect(time).not.toHaveAttribute("tabindex");
    expect(time.closest("td")).toHaveTextContent("Sep 20, 2026, 14:32 UTC");
  });

  it("renders nothing for a missing or unreadable time", () => {
    const { container } = render(
      <>
        <ListTime iso={null} />
        <ListTime iso="soon" />
      </>,
    );
    expect(container.querySelector("time")).toBeNull();
  });
});
