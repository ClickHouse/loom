import { cleanup, fireEvent, render, screen } from "../../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DRAG_TYPE } from "../../lib/useDragHold";
import { DropZone } from "./DropZone";

afterEach(cleanup);

const over = (zone: HTMLElement) => {
  const dataTransfer = { types: ["Files"], dropEffect: "move" };
  fireEvent.dragOver(zone, { dataTransfer });
  return dataTransfer.dropEffect;
};

describe("DropZone", () => {
  it("takes a drop, and says no drop while busy", () => {
    const onDrop = vi.fn();
    const { rerender } = render(
      <DropZone data-testid="zone" onDrop={onDrop}>
        <p>files</p>
      </DropZone>,
    );
    // Idle: the cursor is the browser's, and the zone is tinted as a target.
    expect(over(screen.getByTestId("zone"))).toBe("move");
    expect(screen.getByTestId("zone").className).toContain("bg-accent-soft");
    fireEvent.drop(screen.getByTestId("zone"), { dataTransfer: { types: ["Files"], files: [] } });
    expect(onDrop).toHaveBeenCalledTimes(1);
    rerender(
      <DropZone data-testid="zone" busy onDrop={onDrop}>
        <p>files</p>
      </DropZone>,
    );
    expect(over(screen.getByTestId("zone"))).toBe("none");
    fireEvent.drop(screen.getByTestId("zone"), { dataTransfer: { types: ["Files"], files: [] } });
    expect(onDrop).toHaveBeenCalledTimes(1); // the busy zone hands no drop on
    expect(screen.getByTestId("zone").className).not.toContain("bg-accent-soft");
  });

  it("leaves alone a document or collection the page moves by dragging", () => {
    const onDrop = vi.fn();
    render(
      <DropZone data-testid="zone" onDrop={onDrop}>
        <p>files</p>
      </DropZone>,
    );
    const zone = screen.getByTestId("zone");
    const dataTransfer = { types: [DRAG_TYPE], dropEffect: "move", files: [] };
    // Not cancelled: the browser shows no drop here.
    expect(fireEvent.dragOver(zone, { dataTransfer })).toBe(true);
    expect(zone.className).not.toContain("bg-accent-soft");
    fireEvent.drop(zone, { dataTransfer });
    expect(onDrop).not.toHaveBeenCalled();
  });
});
