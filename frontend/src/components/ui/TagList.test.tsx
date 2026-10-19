import { fireEvent, render, screen } from "../../test/rtl";
import { describe, expect, it, vi } from "vitest";
import { ListRow } from "./ListRow";
import { TagList } from "./TagList";

describe("TagList", () => {
  it("shows three tags, then +N naming the rest; an applied tag reads as pressed", () => {
    const onTagClick = vi.fn();
    render(
      <TagList
        tags={["api", "Limits", "v3", "gateway", "edge"]}
        activeTags={new Set(["limits"])}
        onTagClick={onTagClick}
      />,
    );
    expect(screen.getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
      "Filter by tag api",
      "Filter by tag Limits",
      "Filter by tag v3",
    ]);
    expect(screen.getByRole("button", { name: "Filter by tag Limits" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("2 more tags: gateway, edge")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Filter by tag api" }));
    expect(onTagClick).toHaveBeenCalledWith("api");
  });

  it("renders nothing for a row without tags, so no empty line is left", () => {
    const { container } = render(<TagList tags={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ListRow", () => {
  it("carries its states as data attributes the stylesheet draws", () => {
    render(
      <ListRow active checked deleted data-testid="row">
        body
      </ListRow>,
    );
    const row = screen.getByTestId("row");
    expect(row).toHaveClass("list-row");
    expect(row).toHaveAttribute("data-active");
    expect(row).toHaveAttribute("data-checked");
    expect(row).toHaveAttribute("data-deleted");
  });
});
