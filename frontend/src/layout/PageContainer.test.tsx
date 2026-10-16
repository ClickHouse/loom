import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PageContainer } from "./PageContainer";

// Every dashboard page uses the full width of the content column; the wrapper carries no
// max-width and no centring, only the gutters.

describe("PageContainer", () => {
  it("fills the content column and carries no width cap", () => {
    const { container } = render(
      <PageContainer className="space-y-3">
        <p>x</p>
      </PageContainer>,
    );
    const div = container.firstElementChild;
    expect(div).toBeInstanceOf(HTMLElement);
    if (!(div instanceof HTMLElement)) return;
    expect(div.className).toContain("w-full");
    expect(div.className).toContain("space-y-3");
    expect(div.className).not.toMatch(/\bmax-w-/);
    expect(div.className).not.toContain("mx-auto");
  });
});
