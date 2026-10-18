import { render, screen } from "../../test/rtl";
import { describe, expect, it } from "vitest";
import type { BlacklistRow } from "../../api";
import { PrivacyFilterHits } from "./Blacklist";

const row = (over: Partial<BlacklistRow>): BlacklistRow => ({
  pattern_id: "p1",
  org: "dev",
  namespace: "ns",
  pattern: "sk-",
  pattern_type: "substring",
  scope: "namespace",
  reason: "",
  created_by: "a@x",
  hit_count: 3,
  last_hit_at: "2025-03-04 10:00:00",
  created_at: "2025-01-01T00:00:00Z",
  ...over,
});

// Review finding: a pattern's last hit was the old relative text; it is the list's one time
// format, a <time> with the absolute UTC time behind it.
describe("a privacy filter's hits", () => {
  it("draws the last hit with ListTime", () => {
    render(<PrivacyFilterHits row={row({})} />);
    const time = screen.getByText(/^last/).querySelector("time");
    expect(time).toHaveAttribute("dateTime", "2025-03-04T10:00:00.000Z");
    expect(time).toHaveTextContent("Mar 4, 2025");
    // Quiet in the pattern's row: no tab stop of its own.
    expect(time).not.toHaveAttribute("tabindex");
  });

  it("says when a pattern never matched or its last hit is unknown", () => {
    const { rerender } = render(<PrivacyFilterHits row={row({ hit_count: 0 })} />);
    expect(screen.getByText("never matched")).toBeInTheDocument();
    rerender(<PrivacyFilterHits row={row({ last_hit_at: null })} />);
    expect(screen.getByText("last hit unknown")).toBeInTheDocument();
  });
});
