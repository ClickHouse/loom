import { describe, expect, it } from "vitest";
import { render } from "../../test/rtl";
import { PageHeader } from "./PageHeader";

// WCAG 2.4.2: each page's document title says what it is.
describe("PageHeader", () => {
  it("titles the document after the page, and gives the title back on leaving", () => {
    const { rerender, unmount } = render(<PageHeader title="Runs" />);
    expect(document.title).toBe("Runs · Loom");
    rerender(<PageHeader title="Memories" />);
    expect(document.title).toBe("Memories · Loom");
    unmount();
    expect(document.title).toBe("Loom");
  });

  it("names the tab first when one page has several", () => {
    render(<PageHeader title="Memories" tab="Recall" />);
    expect(document.title).toBe("Recall · Memories · Loom");
  });
});
